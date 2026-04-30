//! Virtual sinks via `pactl load-module module-null-sink` plus a
//! `module-loopback` that forwards the null-sink's monitor to the real
//! headset. This is the Dymstro-era pattern and it's what KDE Plasma's
//! audio applet recognises — `pw-loopback` produced `input.`-prefixed
//! sinks that plasma-pa filters out.

use std::process::{Command, Stdio};

use log::{error, info, warn};

use std::path::PathBuf;

use crate::filter_chain::{FilterChainHandle, FilterChainSpec};
use crate::mic_chain::{MicChainHandle, MicChainSpec};
use crate::protocol::{EqBand, EqChannel, EqState, MicState, NUM_BANDS};
use crate::surround_chain::{SurroundChainHandle, SurroundChainSpec};

pub const GAME_SINK: &str = "SteelGame";
pub const CHAT_SINK: &str = "SteelChat";
pub const MEDIA_SINK: &str = "SteelMedia";
pub const HDMI_SINK: &str = "SteelHDMI";
pub const EQ_GAME_SINK: &str = "SteelGameEQ";
pub const EQ_CHAT_SINK: &str = "SteelChatEQ";
pub const EQ_MEDIA_SINK: &str = "SteelMediaEQ";
pub const EQ_HDMI_SINK: &str = "SteelHDMIEQ";

/// Name suffix the surround filter chain uses for its capture-side
/// sink — apps see it as `effect_input.SteelSurround`. Kept here (not
/// in surround_chain.rs) because audio.rs needs to construct the
/// loopback target string when routing headphone-path channels into
/// the surround chain.
pub const SURROUND_SINK_NAME: &str = "SteelSurround";
pub const OUTPUT_MATCH: &str = "SteelSeries_Arctis_Nova_Pro_Wireless";

/// Every sink-name prefix we're responsible for. Keep this in sync with the
/// *_SINK constants above — it's what the stale-module sweeper and the
/// uninstall scripts use to recognise their targets.
pub const MANAGED_SINK_PREFIX: &str = "Steel";

struct SinkModules {
    null_sink_id: u32,
    loopback_id: u32,
    /// EQ filter chain inserted between this null-sink's monitor and the
    /// headset. When `Some`, the `loopback_id` above points at the EQ
    /// chain's sink instead of the headset directly. The chain itself
    /// has `playback.props.node.target = <headset>` so audio still
    /// reaches the headset — just through the filter graph first.
    eq: Option<EqInsertion>,
}

/// One filter-chain instance owned by a managed pipewire child process.
/// Created when EQ is enabled for a channel; dropped (which kills the
/// child) when EQ is disabled.
struct EqInsertion {
    filter: FilterChainHandle,
}

/// Manages the virtual sinks and their loopbacks. Game + Chat are always
/// created; Media is created when the daemon is launched without
/// `--no-media-sink`. HDMI is created when launched without `--no-hdmi-sink`
/// and an HDMI-capable output sink is detected on the system.
pub struct SinkManager {
    game: Option<SinkModules>,
    chat: Option<SinkModules>,
    media: Option<SinkModules>,
    /// HDMI sink loops to a host-side HDMI output (TV / AVR / monitor speakers),
    /// not to the headset. Independent of headset connection state.
    hdmi: Option<SinkModules>,
    media_enabled: bool,
    hdmi_enabled: bool,
    /// When true, EQ filter chains are inserted between SteelGame /
    /// SteelChat and the headset.
    eq_enabled: bool,
    /// When true, the SteelSurround 7.1 sink + HRIR convolver chain is
    /// loaded. Requires `surround_hrir` to be Some(path) and the file
    /// to exist; if either condition fails, the chain isn't spawned
    /// and the flag stays in the requested state for the next attempt.
    surround_enabled: bool,
    /// User-supplied HRIR WAV path. None means surround is unconfigured
    /// — `enable_surround` will refuse until this is set.
    surround_hrir: Option<PathBuf>,
    /// Live surround chain handle when the chain is running. Dropping
    /// it kills the spawned pipewire child and removes the conf file.
    surround_chain: Option<SurroundChainHandle>,
    /// Full per-channel EQ state: 10 bands per channel, each with its own
    /// frequency / Q / gain / type. Game and Chat tune independently.
    /// Defaults are flat passthrough at standard graphic-EQ frequencies;
    /// preset loads can replace any band's full parameters.
    eq_state: EqState,
    /// Microphone capture-side processing state (gate / NR / AI NC).
    /// Persisted across restarts; the daemon spawns a single
    /// `mic_chain` covering whichever combination is enabled.
    mic_state: MicState,
    /// Live mic chain handle when the chain is running. Dropping it
    /// kills the spawned pipewire child and removes the conf file.
    mic_chain: Option<MicChainHandle>,
    /// Cached hardware microphone source name. Found at headset-
    /// connect time and reused on every mic-state respawn.
    mic_source: Option<String>,
    // Cached during create_sinks so runtime add/remove of the media sink
    // doesn't need to re-query PipeWire for the headset's sink name.
    output_sink: Option<String>,
    // Cached HDMI target so runtime toggles don't re-scan pactl.
    hdmi_target: Option<String>,
}

impl SinkManager {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        media_enabled: bool,
        hdmi_enabled: bool,
        eq_enabled: bool,
        eq_state: EqState,
        surround_enabled: bool,
        surround_hrir: Option<PathBuf>,
        mic_state: MicState,
    ) -> Self {
        // Sweep up anything a previous crash or manual test left behind.
        cleanup_stale_modules();
        SinkManager {
            game: None,
            chat: None,
            media: None,
            hdmi: None,
            media_enabled,
            hdmi_enabled,
            eq_enabled,
            eq_state,
            surround_enabled,
            surround_hrir,
            surround_chain: None,
            mic_state,
            mic_chain: None,
            mic_source: None,
            output_sink: None,
            hdmi_target: None,
        }
    }

    /// Apply a fresh MicState. If the new state has any feature
    /// enabled and we know the mic source, the chain (re)spawns;
    /// otherwise the chain is torn down. Caller is responsible for
    /// emitting the broadcast event.
    pub fn set_mic_state(&mut self, new_state: MicState) {
        self.mic_state = new_state;
        // Tear down the current chain unconditionally — strength
        // changes need a respawn since LADSPA control values are
        // baked into the conf file at spawn time.
        if let Some(handle) = self.mic_chain.take() {
            handle.shutdown();
        }
        let Some(source) = self.mic_source.clone() else {
            // Headset not connected yet. State is stored; the next
            // create_sinks call will pick it up.
            return;
        };
        let spec = MicChainSpec {
            mic_source: &source,
            state: self.mic_state,
        };
        if !spec.has_active_features() {
            return;
        }
        match MicChainHandle::spawn(&spec) {
            Some(handle) => {
                self.mic_chain = Some(handle);
                info!(
                    "Mic chain online (gate={}, nr={}, ai_nc={})",
                    self.mic_state.noise_gate.enabled,
                    self.mic_state.noise_reduction.enabled,
                    self.mic_state.ai_noise_cancellation.enabled,
                );
            }
            None => warn!(
                "Failed to spawn mic chain — see prior warnings (LADSPA plugin missing?)"
            ),
        }
    }

    /// Auto-detect the Arctis Nova Pro Wireless capture (microphone)
    /// source via pactl. Symmetric to `find_output_sink` — looks for
    /// the same OUTPUT_MATCH substring but on the input list. Returns
    /// the source name string, or None if no matching capture device
    /// is connected.
    pub fn find_mic_source() -> Option<String> {
        let output = Command::new("pactl")
            .args(["list", "sources", "short"])
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .output()
            .ok()?;
        let stdout = String::from_utf8_lossy(&output.stdout);
        for line in stdout.lines() {
            if line.contains(OUTPUT_MATCH)
                && line.contains("input")
                && !line.contains("monitor")
            {
                if let Some(name) = line.split('\t').nth(1) {
                    return Some(name.to_string());
                }
            }
        }
        None
    }

    /// Update the HRIR file path. Three cases to handle:
    ///   1. Chain currently running: restart with the new file so the
    ///      change takes effect immediately.
    ///   2. Chain not running but `surround_enabled` is true (the
    ///      common "default-on, GUI just delivered the bundled HRIR"
    ///      case): spawn the chain now and rewire loopbacks.
    ///   3. Path cleared (None): if a chain was running it shuts down
    ///      and `surround_enabled` flips off so the GUI Status stays
    ///      consistent.
    ///
    /// Returns the path as actually stored.
    pub fn set_surround_hrir(&mut self, path: Option<PathBuf>) -> Option<PathBuf> {
        // Strip empty strings to None so the GUI doesn't have to.
        let cleaned = path.filter(|p| !p.as_os_str().is_empty());
        self.surround_hrir = cleaned.clone();

        let was_running = self.surround_chain.is_some();
        if was_running {
            if let Some(handle) = self.surround_chain.take() {
                handle.shutdown();
            }
            if cleaned.is_some() {
                self.spawn_surround_chain();
                if self.surround_chain.is_some() {
                    self.rewire_all_headphone_channels();
                }
            } else {
                // Path cleared while running → surround can no longer
                // run. Flip the flag and re-route loopbacks back to
                // the headset.
                self.surround_enabled = false;
                self.rewire_all_headphone_channels();
            }
        } else if cleaned.is_some() && self.surround_enabled {
            // Chain wasn't running because we had no HRIR; now we do.
            // Bring it up and wire the headphone-path channels in.
            self.spawn_surround_chain();
            if self.surround_chain.is_some() {
                self.rewire_all_headphone_channels();
            }
        }
        cleaned
    }

    /// Toggle the surround chain. When enabling, the chain spawns and
    /// every headphone-path loopback / EQ chain is re-routed to feed
    /// SteelSurround instead of the headset. When disabling, loopbacks
    /// re-route back to the headset before the surround chain shuts
    /// down so there's no audio gap during the swap.
    ///
    /// Returns the actual state after the attempt — if `enable=true`
    /// is passed but no HRIR is configured (or the file doesn't
    /// exist), the call is logged and false is returned without
    /// changing state.
    pub fn set_surround_enabled(&mut self, enable: bool) -> bool {
        if enable {
            let Some(path) = self.surround_hrir.clone() else {
                warn!("Cannot enable surround: no HRIR file configured");
                return false;
            };
            if !path.is_file() {
                warn!(
                    "Cannot enable surround: HRIR file {} not found",
                    path.display()
                );
                return false;
            }
            if self.surround_chain.is_some() {
                self.surround_enabled = true;
                return true;
            }
            self.surround_enabled = true;
            self.spawn_surround_chain();
            if self.surround_chain.is_some() {
                // Now that headphone_path_target returns the surround
                // capture sink, rewire every Game / Chat / Media
                // downstream so audio flows through the HRIR convolver.
                self.rewire_all_headphone_channels();
                true
            } else {
                // Spawn failed — drop the flag so the next click
                // triggers a fresh attempt instead of looking like
                // surround is on with no chain.
                self.surround_enabled = false;
                false
            }
        } else {
            // Order matters: drop the chain handle FIRST so
            // headphone_path_target() returns the headset, then rewire
            // loopbacks. The taken handle gets shut down after the
            // rewire so the chain stays alive while it had clients,
            // even though those clients no longer feed it.
            self.surround_enabled = false;
            let chain = self.surround_chain.take();
            self.rewire_all_headphone_channels();
            if let Some(handle) = chain {
                handle.shutdown();
            }
            false
        }
    }

    fn spawn_surround_chain(&mut self) {
        let Some(path) = self.surround_hrir.clone() else {
            return;
        };
        let Some(headset) = self.output_sink.clone() else {
            // No headset yet — leave the flag set so the next
            // `create_sinks` re-spawns once the headset is detected.
            return;
        };
        let spec = SurroundChainSpec {
            hrir_path: &path,
            playback_target: &headset,
        };
        match SurroundChainHandle::spawn(&spec) {
            Some(handle) => {
                self.surround_chain = Some(handle);
                info!(
                    "Surround chain online (HRIR: {})",
                    path.display()
                );
            }
            None => {
                warn!(
                    "Failed to spawn surround chain — see prior warnings"
                );
            }
        }
    }

    /// Where the headphone-path channels (Game / Chat / Media) should
    /// route their downstream audio. When the surround chain is up,
    /// loopbacks / EQ chains target the surround capture sink so every
    /// channel gets HRIR'd before reaching the headset. Otherwise they
    /// target the headset directly. HDMI is intentionally NOT routed
    /// through surround — it goes to a host HDMI output (TV / AVR)
    /// which already handles surround on its own.
    fn headphone_path_target(&self) -> Option<String> {
        if self.surround_chain.is_some() {
            Some(format!("effect_input.{}", SURROUND_SINK_NAME))
        } else {
            self.output_sink.clone()
        }
    }

    /// Tear down + recreate the loopback (or EQ chain) for a single
    /// headphone-path channel. Called when the surround chain comes
    /// up / down so the channel's downstream follows the new target.
    /// HDMI passes through unchanged — its target is the host HDMI
    /// output, not the headset.
    fn rewire_one_headphone_channel(&mut self, channel: EqChannel) {
        if matches!(channel, EqChannel::Hdmi) {
            return;
        }
        // EQ-on path: the chain's downstream is read from eq_routing,
        // which itself reads headphone_path_target — so a respawn is
        // all we need to follow the new target.
        if self.eq_enabled {
            self.respawn_channel_chain(channel);
            return;
        }
        // EQ-off path: just the bare loopback. Unload + reload with the
        // new target.
        let Some(target) = self.headphone_path_target() else {
            return;
        };
        let (slot, name) = match channel {
            EqChannel::Game => (self.game.as_mut(), GAME_SINK),
            EqChannel::Chat => (self.chat.as_mut(), CHAT_SINK),
            EqChannel::Media => (self.media.as_mut(), MEDIA_SINK),
            EqChannel::Hdmi => unreachable!(),
        };
        let Some(s) = slot else {
            return;
        };
        unload_module(s.loopback_id);
        if let Some(id) = load_module(&[
            "module-loopback",
            &format!("source={name}.monitor"),
            &format!("sink={target}"),
            "latency_msec=1",
        ]) {
            s.loopback_id = id;
        } else {
            warn!(
                "Failed to re-target loopback for {} → {}",
                name, target
            );
        }
    }

    fn rewire_all_headphone_channels(&mut self) {
        for ch in [EqChannel::Game, EqChannel::Chat, EqChannel::Media] {
            self.rewire_one_headphone_channel(ch);
        }
    }

    /// Reset every runtime preference to its factory default and tear
    /// down anything that was running. Used by the GUI's "Reset to
    /// defaults" button. Headset connection state is preserved — we
    /// don't yank the HID device or destroy the user-facing null
    /// sinks; we just remove the EQ chains, surround chain, and the
    /// optional Media + HDMI sinks, then restore eq_state to flat.
    /// The caller is responsible for persisting the new state and
    /// broadcasting events.
    pub fn reset_to_defaults(&mut self) {
        // Tear down EQ chains so we can rewire loopbacks cleanly.
        if self.eq_enabled {
            self.disable_eq();
        }
        // Tear down surround so headphone-path loopbacks return to
        // the headset (and so the chain process doesn't keep running
        // with stale state when surround is re-enabled later).
        if self.surround_chain.is_some() || self.surround_enabled {
            // Force-disable irrespective of flag state — this calls
            // rewire_all_headphone_channels for us.
            let _ = self.set_surround_enabled(false);
        }
        // Drop the optional Media + HDMI sinks.
        if self.media.is_some() {
            self.disable_media();
        }
        if self.hdmi.is_some() {
            self.disable_hdmi();
        }
        // Reset eq_state itself (the per-band data) to flat. We don't
        // need to respawn anything — EQ is already off.
        self.eq_state = EqState::default();
        // Clear surround config — the GUI's reset will re-send the
        // bundled HRIR path on next launch via the
        // surround_default_applied marker reset.
        self.surround_hrir = None;
        // Clear mic processing too — set_mic_state with a default
        // (all-disabled) MicState shuts the chain down if it was
        // running and resets the persisted state.
        self.set_mic_state(MicState::default());
        info!("SinkManager state reset to defaults");
    }

    /// Read-only view of the current per-channel EQ state. The daemon
    /// snapshot logic copies this out for status events.
    pub fn eq_state(&self) -> EqState {
        self.eq_state
    }

    /// Update one band's gain on one channel (band is 1-indexed, 1..=10).
    /// Out-of-range bands or NaN gains are rejected. Returns the
    /// (possibly clamped) new value applied. If EQ is currently enabled,
    /// only the affected channel's chain respawns — Game stays untouched
    /// when you tweak Chat and vice versa. The other band parameters
    /// (freq, Q, type) are preserved — a slider drag only moves gain.
    pub fn set_eq_band_gain(
        &mut self,
        channel: EqChannel,
        band: u8,
        gain_db: f32,
    ) -> Option<f32> {
        if !(1..=NUM_BANDS as u8).contains(&band) || !gain_db.is_finite() {
            return None;
        }
        let clamped = gain_db.clamp(-12.0, 12.0);
        let idx = (band - 1) as usize;
        let arr = self.eq_state.for_channel_mut(channel);
        if (arr[idx].gain - clamped).abs() < 1e-6 {
            // No change — skip the chain respawn cost.
            return Some(clamped);
        }
        arr[idx].gain = clamped;
        self.respawn_channel_chain(channel);
        Some(clamped)
    }

    /// Replace one band's full parameters wholesale. Used when a preset
    /// loads, where freq, Q, gain and type may all change at once.
    /// Out-of-range bands rejected. Gain is clamped to [-12, 12] dB;
    /// freq is clamped to a safe audible range. Returns the band as
    /// actually applied (after clamping).
    pub fn set_eq_band(
        &mut self,
        channel: EqChannel,
        band: u8,
        params: EqBand,
    ) -> Option<EqBand> {
        if !(1..=NUM_BANDS as u8).contains(&band) {
            return None;
        }
        if !params.freq.is_finite() || !params.q.is_finite() || !params.gain.is_finite() {
            return None;
        }
        let mut clean = params;
        clean.gain = clean.gain.clamp(-12.0, 12.0);
        clean.freq = clean.freq.clamp(20.0, 20_000.0);
        clean.q = clean.q.max(0.05);

        let idx = (band - 1) as usize;
        let arr = self.eq_state.for_channel_mut(channel);
        if arr[idx] == clean {
            return Some(clean);
        }
        arr[idx] = clean;
        self.respawn_channel_chain(channel);
        Some(clean)
    }

    /// Replace every band on a channel in one shot — used by preset
    /// loads. Sending 10 SetEqBand calls would respawn the chain 10
    /// times and emit 10 broadcast events; this batches into a single
    /// respawn + caller emits one event. Each band's freq/q/gain are
    /// clamped to their safe ranges (same as `set_eq_band`). Returns
    /// the bands as actually stored (post-clamp).
    pub fn set_eq_channel_bands(
        &mut self,
        channel: EqChannel,
        bands: [EqBand; NUM_BANDS],
    ) -> Option<[EqBand; NUM_BANDS]> {
        let mut clean: [EqBand; NUM_BANDS] = bands;
        for b in clean.iter_mut() {
            if !b.freq.is_finite() || !b.q.is_finite() || !b.gain.is_finite() {
                return None;
            }
            b.gain = b.gain.clamp(-12.0, 12.0);
            b.freq = b.freq.clamp(20.0, 20_000.0);
            b.q = b.q.max(0.05);
        }
        let arr = self.eq_state.for_channel_mut(channel);
        if *arr == clean {
            return Some(clean);
        }
        *arr = clean;
        self.respawn_channel_chain(channel);
        Some(clean)
    }

    /// Tear down the current EQ chain on `channel` and re-insert one
    /// driven by the latest `eq_state`. No-op if EQ is currently
    /// disabled, the channel's null-sink isn't loaded (e.g. user hasn't
    /// enabled the Media sink), or the chain's downstream target isn't
    /// known — the new state is still stored, and `create_sinks` /
    /// `enable_media` / `enable_hdmi` will pick it up on next connect.
    fn respawn_channel_chain(&mut self, channel: EqChannel) {
        if !self.eq_enabled {
            return;
        }
        let new_bands = self.eq_state.for_channel(channel);
        let routing = self.eq_routing(channel);
        let Some((slot, null_name, eq_name, eq_desc, target)) = routing else {
            return;
        };
        let _ = remove_eq_from_channel(slot, null_name, &target);
        insert_eq_into_channel(slot, null_name, eq_name, eq_desc, &target, &new_bands);
    }

    /// Look up everything needed to (re)insert an EQ chain for a given
    /// channel: the SinkModules slot, the user-facing null-sink name,
    /// the EQ sink name + description, and the downstream playback
    /// target (headset for Game/Chat/Media; HDMI output for Hdmi).
    /// Returns None when the channel's null-sink isn't loaded or when
    /// the downstream target hasn't been resolved yet.
    fn eq_routing(
        &mut self,
        channel: EqChannel,
    ) -> Option<(&mut SinkModules, &'static str, &'static str, &'static str, String)> {
        match channel {
            EqChannel::Game => {
                // headphone_path_target returns the surround capture
                // sink when surround is active so the EQ chain feeds
                // surround instead of the headset directly. Falls back
                // to the headset when surround is off.
                let target = self.headphone_path_target()?;
                let slot = self.game.as_mut()?;
                Some((
                    slot,
                    GAME_SINK,
                    EQ_GAME_SINK,
                    "SteelVoiceMix Game EQ",
                    target,
                ))
            }
            EqChannel::Chat => {
                let target = self.headphone_path_target()?;
                let slot = self.chat.as_mut()?;
                Some((
                    slot,
                    CHAT_SINK,
                    EQ_CHAT_SINK,
                    "SteelVoiceMix Chat EQ",
                    target,
                ))
            }
            EqChannel::Media => {
                let target = self.headphone_path_target()?;
                let slot = self.media.as_mut()?;
                Some((
                    slot,
                    MEDIA_SINK,
                    EQ_MEDIA_SINK,
                    "SteelVoiceMix Media EQ",
                    target,
                ))
            }
            EqChannel::Hdmi => {
                // HDMI loops to a host-side HDMI output, NOT the headset.
                // Bypasses the surround chain (TV/AVR handles surround
                // natively).
                let target = self.hdmi_target.clone()?;
                let slot = self.hdmi.as_mut()?;
                Some((
                    slot,
                    HDMI_SINK,
                    EQ_HDMI_SINK,
                    "SteelVoiceMix HDMI EQ",
                    target,
                ))
            }
        }
    }

    /// Whether the SteelMedia sink is currently requested (may be idle if
    /// the daemon is disconnected; the sink only materialises when
    /// `create_sinks` runs against a live headset).
    pub fn media_enabled(&self) -> bool {
        self.media_enabled
    }

    /// Runtime toggle: add the SteelMedia sink if we're connected, or record
    /// the intent for the next connect if we're not. Returns the new state
    /// so callers don't have to re-query.
    pub fn enable_media(&mut self) -> bool {
        self.media_enabled = true;
        if self.media.is_none() {
            // Loopback target follows surround state — feeds the
            // surround capture sink when surround is up so Media
            // gets HRIR'd alongside Game / Chat.
            if let Some(target) = self.headphone_path_target() {
                self.media = create_sink_pair(&target, MEDIA_SINK, "SteelMedia");
            }
        }
        // If EQ is on, the freshly-loaded Media null-sink should pick up
        // its filter chain immediately rather than waiting for the next
        // disable/enable cycle. respawn covers the install-then-insert
        // case via eq_routing.
        if self.eq_enabled && self.media.is_some() {
            self.respawn_channel_chain(EqChannel::Media);
        }
        true
    }

    /// Runtime toggle: tear down the SteelMedia sink immediately (even while
    /// connected) and remember that future connects should skip it.
    pub fn disable_media(&mut self) -> bool {
        self.media_enabled = false;
        if let Some(m) = self.media.take() {
            unload_module(m.loopback_id);
            unload_module(m.null_sink_id);
        }
        false
    }

    /// Whether the SteelHDMI sink is currently requested.
    pub fn hdmi_enabled(&self) -> bool {
        self.hdmi_enabled
    }

    /// Runtime toggle: add the SteelHDMI sink, looping to a host HDMI output.
    /// Re-scans pactl for an HDMI sink each time it's enabled — the user may
    /// have plugged in a TV/AVR after the daemon started.
    pub fn enable_hdmi(&mut self) -> bool {
        self.hdmi_enabled = true;
        if self.hdmi.is_none() {
            let target = self.hdmi_target.clone().or_else(Self::find_hdmi_sink);
            match target {
                Some(t) => {
                    self.hdmi_target = Some(t.clone());
                    self.hdmi = create_sink_pair(&t, HDMI_SINK, "SteelHDMI");
                }
                None => warn!("HDMI sink requested but no HDMI output detected"),
            }
        }
        if self.eq_enabled && self.hdmi.is_some() {
            self.respawn_channel_chain(EqChannel::Hdmi);
        }
        true
    }

    /// Runtime toggle: tear down the SteelHDMI sink and remember the user's
    /// off-preference for next start.
    pub fn disable_hdmi(&mut self) -> bool {
        self.hdmi_enabled = false;
        if let Some(h) = self.hdmi.take() {
            unload_module(h.loopback_id);
            unload_module(h.null_sink_id);
        }
        false
    }

    /// Runtime toggle: insert a filter chain between every loaded
    /// virtual sink and its downstream target. The user-facing null-
    /// sinks themselves stay loaded — only the loopback target changes
    /// — so apps bound to SteelGame (Discord, OBS, …) keep their
    /// connection across this toggle. Channels whose null-sinks aren't
    /// loaded yet (e.g. Media when the user hasn't opted into it) are
    /// skipped silently; the next `create_sinks` / `enable_media` /
    /// `enable_hdmi` call will pick them up via the same flag.
    pub fn enable_eq(&mut self) -> bool {
        // Without the headset (the Game/Chat/Media downstream target),
        // we can't insert anything yet — record the intent and bail.
        if self.output_sink.is_none() {
            warn!("EQ enable requested but no headset connected yet — will retry when sinks are created");
            self.eq_enabled = true;
            return true;
        }

        // Walk every channel; skip the ones whose sink isn't loaded.
        // If any later channel fails, roll back the earlier successes
        // so we don't leave the daemon in a half-EQ state.
        let mut applied: Vec<EqChannel> = Vec::new();
        for ch in [
            EqChannel::Game,
            EqChannel::Chat,
            EqChannel::Media,
            EqChannel::Hdmi,
        ] {
            let bands = self.eq_state.for_channel(ch);
            let routing = self.eq_routing(ch);
            let Some((slot, null_name, eq_name, eq_desc, target)) = routing else {
                continue;
            };
            let ok = insert_eq_into_channel(
                slot, null_name, eq_name, eq_desc, &target, &bands,
            );
            if ok {
                applied.push(ch);
            } else {
                // Roll back any chains we already inserted this call.
                for done in applied.drain(..) {
                    if let Some((slot, null_name, _, _, target)) = self.eq_routing(done) {
                        let _ = remove_eq_from_channel(slot, null_name, &target);
                    }
                }
                return false;
            }
        }

        self.eq_enabled = true;
        info!(
            "EQ enabled (filter chains on: {})",
            applied
                .iter()
                .map(|c| format!("{c:?}"))
                .collect::<Vec<_>>()
                .join(", ")
        );
        true
    }

    /// Runtime toggle: tear down every active EQ filter chain and reroute
    /// loopbacks back to their direct downstream targets.
    pub fn disable_eq(&mut self) -> bool {
        self.eq_enabled = false;
        for ch in [
            EqChannel::Game,
            EqChannel::Chat,
            EqChannel::Media,
            EqChannel::Hdmi,
        ] {
            if let Some((slot, null_name, _, _, target)) = self.eq_routing(ch) {
                let _ = remove_eq_from_channel(slot, null_name, &target);
            } else {
                // No null-sink for this channel right now — clear any
                // stale eq insertion defensively if a slot exists.
                let slot = match ch {
                    EqChannel::Game => self.game.as_mut(),
                    EqChannel::Chat => self.chat.as_mut(),
                    EqChannel::Media => self.media.as_mut(),
                    EqChannel::Hdmi => self.hdmi.as_mut(),
                };
                if let Some(s) = slot {
                    s.eq = None;
                }
            }
        }
        info!("EQ disabled (all channels reverted to direct routing)");
        false
    }

    /// Auto-detect the Nova Pro Wireless PipeWire output sink name.
    pub fn find_output_sink() -> Option<String> {
        let output = Command::new("pactl")
            .args(["list", "sinks", "short"])
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .output()
            .ok()?;

        let stdout = String::from_utf8_lossy(&output.stdout);
        for line in stdout.lines() {
            if line.contains(OUTPUT_MATCH) && !line.contains("input.") {
                if let Some(name) = line.split('\t').nth(1) {
                    return Some(name.to_string());
                }
            }
        }
        None
    }

    /// Auto-detect a PipeWire HDMI output sink. Picks the first match —
    /// systems with multiple HDMI outputs (multi-GPU, multi-monitor) may
    /// need a future config knob to override this. Skips `input.`-prefixed
    /// virtual nodes so plasma-pa-style filters don't trip the heuristic.
    pub fn find_hdmi_sink() -> Option<String> {
        let output = Command::new("pactl")
            .args(["list", "sinks", "short"])
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .output()
            .ok()?;

        let stdout = String::from_utf8_lossy(&output.stdout);
        for line in stdout.lines() {
            let lower = line.to_lowercase();
            if lower.contains("hdmi") && !lower.contains("input.") {
                if let Some(name) = line.split('\t').nth(1) {
                    return Some(name.to_string());
                }
            }
        }
        None
    }

    /// Create the virtual sinks routing to the given output sink.
    pub fn create_sinks(&mut self, output_sink: &str) {
        self.destroy_sinks();
        self.output_sink = Some(output_sink.to_string());

        // If surround was on before disconnect (or set via persisted
        // state), bring its chain up FIRST — that way the loopbacks
        // we create just below can target SteelSurround's capture
        // sink directly, no rewiring needed. If the chain fails to
        // spawn (HRIR missing, etc.) headphone_path_target falls back
        // to the headset and we get plain stereo routing.
        if self.surround_enabled
            && self.surround_chain.is_none()
            && self.surround_hrir.is_some()
        {
            self.spawn_surround_chain();
        }
        let headphone_target = self
            .headphone_path_target()
            .unwrap_or_else(|| output_sink.to_string());

        // Descriptions cannot contain spaces — pactl's proplist parser
        // splits sink_properties tokens on whitespace with no quote or
        // escape handling, so "Steel Game" would truncate to "Steel".
        // Matching the sink name (no separator) also avoids cognitive
        // mismatch with `pactl list short sinks` output.
        self.game = create_sink_pair(&headphone_target, GAME_SINK, "SteelGame");
        self.chat = create_sink_pair(&headphone_target, CHAT_SINK, "SteelChat");
        // Media sink mirrors Game/Chat structurally but is deliberately
        // ignored by the ChatMix dial handler — its volume stays at whatever
        // KDE/pactl set. Use case: music and browser audio that shouldn't
        // dip when the user biases the dial toward chat.
        if self.media_enabled {
            self.media = create_sink_pair(&headphone_target, MEDIA_SINK, "SteelMedia");
        }
        // HDMI loopback target is independent of the headset — it goes to the
        // host-side HDMI sink (TV / AVR / monitor speakers). HDMI also
        // bypasses the surround chain because the downstream device
        // (TV / AVR) handles surround natively. Detect at create time
        // so toggling the headset doesn't lose the HDMI route.
        if self.hdmi_enabled {
            if let Some(hdmi_target) = Self::find_hdmi_sink() {
                self.hdmi_target = Some(hdmi_target.clone());
                self.hdmi = create_sink_pair(&hdmi_target, HDMI_SINK, "SteelHDMI");
            } else {
                warn!("HDMI sink enabled but no HDMI output sink detected");
            }
        }

        let core_ok = self.game.is_some() && self.chat.is_some();
        if core_ok {
            let mut active = vec![GAME_SINK, CHAT_SINK];
            if self.media.is_some() {
                active.push(MEDIA_SINK);
            }
            if self.hdmi.is_some() {
                active.push(HDMI_SINK);
            }
            info!(
                "Created sinks: {} (headphone target: {})",
                active.join(", "),
                headphone_target,
            );

            // If EQ was on before disconnect (or set via persisted state),
            // re-insert the filter chains now that the sinks exist again.
            // Reuses the same per-channel helper used by the runtime
            // enable_eq path so all four channel types get covered.
            if self.eq_enabled {
                for ch in [
                    EqChannel::Game,
                    EqChannel::Chat,
                    EqChannel::Media,
                    EqChannel::Hdmi,
                ] {
                    let bands = self.eq_state.for_channel(ch);
                    if let Some((slot, null_name, eq_name, eq_desc, target)) =
                        self.eq_routing(ch)
                    {
                        insert_eq_into_channel(
                            slot, null_name, eq_name, eq_desc, &target, &bands,
                        );
                    }
                }
            }
        } else {
            error!("Failed to create one or more sinks");
        }

        // Discover the hardware mic source — it has the same
        // OUTPUT_MATCH substring as the headset output but on the
        // pactl sources list. Then re-spawn the mic chain if the
        // user has any feature enabled. Done last so the chain
        // doesn't try to spawn before the headset is fully detected.
        if let Some(source) = Self::find_mic_source() {
            info!("Detected microphone source: {source}");
            self.mic_source = Some(source);
            // Re-apply the persisted mic_state — set_mic_state shuts
            // down any existing chain, so it's safe to call even if
            // the chain came up earlier somehow.
            if self.mic_chain.is_none() {
                let saved = self.mic_state;
                self.set_mic_state(saved);
            }
        } else {
            warn!("Microphone source not found — mic processing disabled until reconnect");
        }
    }

    /// Unload the null-sink + loopback modules we created. Also tears down
    /// any inserted EQ filter chains by dropping their handles (which kills
    /// the spawned pipewire children).
    pub fn destroy_sinks(&mut self) {
        for slot in [&mut self.game, &mut self.chat, &mut self.media, &mut self.hdmi] {
            if let Some(mut m) = slot.take() {
                if let Some(eq) = m.eq.take() {
                    eq.filter.shutdown();
                }
                unload_module(m.loopback_id);
                unload_module(m.null_sink_id);
            }
        }
        // Surround chain owns its own pipewire child + null-sink; drop
        // the handle and let its Drop impl clean up.
        if let Some(handle) = self.mic_chain.take() {
            handle.shutdown();
        }
        self.mic_source = None;
        if let Some(handle) = self.surround_chain.take() {
            handle.shutdown();
        }
        self.output_sink = None;
        self.hdmi_target = None;
    }

    /// Set volume on a sink (0–100) by its sink name.
    pub fn set_volume(sink: &str, volume: u8) {
        let vol_str = format!("{volume}%");
        let result = Command::new("pactl")
            .args(["set-sink-volume", sink, &vol_str])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn();

        if let Err(e) = result {
            warn!("Failed to set volume on {sink}: {e}");
        }
    }
}

impl Drop for SinkManager {
    fn drop(&mut self) {
        self.destroy_sinks();
    }
}

fn create_sink_pair(target: &str, name: &str, description: &str) -> Option<SinkModules> {
    // sink_properties is parsed token-by-token on spaces with no escape
    // or quote support. Values must be a single whitespace-free token.
    // node.description is the primary display string in plasma-pa;
    // node.nick is a secondary hint some UIs prefer.
    let null_sink_id = load_module(&[
        "module-null-sink",
        &format!("sink_name={name}"),
        &format!(
            "sink_properties=node.description={description} node.nick={description} device.description={description}"
        ),
    ])?;

    match load_module(&[
        "module-loopback",
        &format!("source={name}.monitor"),
        &format!("sink={target}"),
        "latency_msec=1",
    ]) {
        Some(loopback_id) => Some(SinkModules {
            null_sink_id,
            loopback_id,
            eq: None,
        }),
        None => {
            warn!("Loopback for {name} failed — unloading its null-sink");
            unload_module(null_sink_id);
            None
        }
    }
}

/// Insert an EQ filter chain in front of a channel's headset path.
/// Spawns a managed pipewire child that hosts the chain (which auto-routes
/// to the headset via its own `playback.props.node.target`), then swaps
/// the channel's null-sink → headset loopback for a null-sink → EQ-sink
/// loopback. The null-sink module ID never changes, so apps bound to
/// the user-facing sink (Discord, OBS) keep their connection.
///
/// Idempotent: calling it on a channel that already has EQ inserted is
/// a no-op that returns true.
fn insert_eq_into_channel(
    channel: &mut SinkModules,
    null_sink_name: &str,
    eq_sink_name: &str,
    eq_description: &str,
    headset: &str,
    bands: &[EqBand; NUM_BANDS],
) -> bool {
    if channel.eq.is_some() {
        return true;
    }

    let spec = FilterChainSpec {
        sink_name: eq_sink_name,
        description: eq_description,
        playback_target: headset,
        bands: *bands,
    };
    // Capture the prefixed sink name BEFORE moving spec into spawn — used
    // below as the loopback target. `effect_input.<name>` is the prefix
    // PipeWire's convention reserves for filter-chain inputs; plasma-pa
    // hides it from the user-facing audio device list.
    let capture_sink = spec.capture_sink();
    let Some(filter) = FilterChainHandle::spawn(&spec) else {
        return false;
    };

    // FilterChainHandle::spawn already waited for the chain's nodes to
    // register and explicitly linked the chain output to the headset.
    // Safe to point our loopback at the chain sink now.

    // Tear down the existing direct-to-headset loopback.
    unload_module(channel.loopback_id);

    // Establish the new null-sink → EQ-sink loopback.
    let new_loopback = load_module(&[
        "module-loopback",
        &format!("source={null_sink_name}.monitor"),
        &format!("sink={capture_sink}"),
        "latency_msec=1",
    ]);

    match new_loopback {
        Some(id) => {
            channel.loopback_id = id;
            channel.eq = Some(EqInsertion { filter });
            info!("Inserted EQ chain '{eq_sink_name}' for {null_sink_name}");
            true
        }
        None => {
            // Loopback to EQ sink failed — fall back to direct so audio
            // still works, and shut down the orphan filter chain.
            warn!("Failed to retarget {null_sink_name} loopback at {eq_sink_name}; reverting to direct");
            let direct = load_module(&[
                "module-loopback",
                &format!("source={null_sink_name}.monitor"),
                &format!("sink={headset}"),
                "latency_msec=1",
            ]);
            if let Some(id) = direct {
                channel.loopback_id = id;
            }
            filter.shutdown();
            false
        }
    }
}

/// Reverse `insert_eq_into_channel`: tear down the EQ chain and restore
/// the channel's direct null-sink → headset loopback. Idempotent.
fn remove_eq_from_channel(
    channel: &mut SinkModules,
    null_sink_name: &str,
    headset: &str,
) -> bool {
    let Some(eq) = channel.eq.take() else {
        return true;
    };

    // Tear down the null-sink → EQ-sink loopback first.
    unload_module(channel.loopback_id);

    // Restore the direct null-sink → headset loopback.
    let direct = load_module(&[
        "module-loopback",
        &format!("source={null_sink_name}.monitor"),
        &format!("sink={headset}"),
        "latency_msec=1",
    ]);
    if let Some(id) = direct {
        channel.loopback_id = id;
    } else {
        warn!("Failed to restore direct loopback for {null_sink_name}; channel may be silent until reconnect");
    }

    // Now safe to kill the filter-chain child.
    eq.filter.shutdown();
    info!("Removed EQ chain for {null_sink_name}");
    true
}

fn load_module(args: &[&str]) -> Option<u32> {
    let output = Command::new("pactl")
        .arg("load-module")
        .args(args)
        .stderr(Stdio::null())
        .output()
        .ok()?;
    if !output.status.success() {
        warn!("pactl load-module failed: {:?}", args);
        return None;
    }
    String::from_utf8_lossy(&output.stdout)
        .trim()
        .parse::<u32>()
        .ok()
}

fn unload_module(id: u32) {
    let _ = Command::new("pactl")
        .args(["unload-module", &id.to_string()])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
}

/// Unload any SteelGame / SteelChat modules leaked by a previous run (crash,
/// kill -9, manual test). We identify them by their loaded arguments.
fn cleanup_stale_modules() {
    let Ok(output) = Command::new("pactl").args(["list", "modules"]).output() else {
        return;
    };
    let stdout = String::from_utf8_lossy(&output.stdout);
    let mut current_id: Option<u32> = None;
    for line in stdout.lines() {
        if let Some(rest) = line.strip_prefix("Module #") {
            current_id = rest.trim().parse::<u32>().ok();
            continue;
        }
        let trimmed = line.trim();
        if !trimmed.starts_with("Argument:") {
            continue;
        }
        // Match any module whose argument references one of our sink
        // prefixes. Catches Game/Chat/Media without listing each by name.
        // Legacy "Nova" prefix included so upgrading from pre-rename
        // installs also sweeps the orphans.
        let prefix_match = trimmed.contains(&format!("sink_name={MANAGED_SINK_PREFIX}"))
            || trimmed.contains(&format!("source={MANAGED_SINK_PREFIX}"))
            || trimmed.contains("sink_name=Nova")
            || trimmed.contains("source=Nova");
        if prefix_match {
            if let Some(id) = current_id {
                info!("Unloading stale managed module #{id}");
                unload_module(id);
            }
        }
    }
}
