//! Virtual sinks via `pactl load-module module-null-sink` plus a
//! `module-loopback` that forwards the null-sink's monitor to the real
//! headset. This is the Dymstro-era pattern and it's what KDE Plasma's
//! audio applet recognises — `pw-loopback` produced `input.`-prefixed
//! sinks that plasma-pa filters out.

use std::process::{Command, Stdio};

use log::{error, info, warn};

use crate::filter_chain::{FilterChainHandle, FilterChainSpec};

pub const GAME_SINK: &str = "SteelGame";
pub const CHAT_SINK: &str = "SteelChat";
pub const MEDIA_SINK: &str = "SteelMedia";
pub const HDMI_SINK: &str = "SteelHDMI";
pub const EQ_GAME_SINK: &str = "SteelGameEQ";
pub const EQ_CHAT_SINK: &str = "SteelChatEQ";
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
    /// SteelChat and the headset. The chain currently passes audio
    /// through unchanged (Phase 1 scaffolding); real biquad bands will
    /// land in a later commit.
    eq_enabled: bool,
    // Cached during create_sinks so runtime add/remove of the media sink
    // doesn't need to re-query PipeWire for the headset's sink name.
    output_sink: Option<String>,
    // Cached HDMI target so runtime toggles don't re-scan pactl.
    hdmi_target: Option<String>,
}

impl SinkManager {
    pub fn new(media_enabled: bool, hdmi_enabled: bool, eq_enabled: bool) -> Self {
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
            output_sink: None,
            hdmi_target: None,
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
            if let Some(out) = self.output_sink.clone() {
                self.media = create_sink_pair(&out, MEDIA_SINK, "SteelMedia");
            }
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

    /// Runtime toggle: insert a filter chain between SteelGame/SteelChat and
    /// the headset. The user-facing null-sinks themselves stay loaded — only
    /// the loopback target changes — so apps bound to SteelGame (Discord,
    /// OBS, …) keep their connection across this toggle.
    pub fn enable_eq(&mut self) -> bool {
        if self.eq_enabled && self.game.as_ref().is_some_and(|m| m.eq.is_some()) {
            return true;
        }
        let Some(headset) = self.output_sink.clone() else {
            warn!("EQ enable requested but no headset connected yet — will retry when sinks are created");
            self.eq_enabled = true;
            return true;
        };

        // Try to insert on each existing channel. If chat fails after game
        // succeeds, undo game so we end in a consistent state.
        let game_ok = match self.game.as_mut() {
            Some(ch) => insert_eq_into_channel(
                ch,
                GAME_SINK,
                EQ_GAME_SINK,
                "SteelVoiceMix Game EQ",
                &headset,
            ),
            None => true,
        };
        if !game_ok {
            return false;
        }

        let chat_ok = match self.chat.as_mut() {
            Some(ch) => insert_eq_into_channel(
                ch,
                CHAT_SINK,
                EQ_CHAT_SINK,
                "SteelVoiceMix Chat EQ",
                &headset,
            ),
            None => true,
        };
        if !chat_ok {
            if let Some(ch) = self.game.as_mut() {
                let _ = remove_eq_from_channel(ch, GAME_SINK, &headset);
            }
            return false;
        }

        self.eq_enabled = true;
        info!("EQ enabled (Game + Chat routed through filter chains)");
        true
    }

    /// Runtime toggle: tear down the EQ filter chains and reroute Game/Chat
    /// loopbacks back to the headset directly.
    pub fn disable_eq(&mut self) -> bool {
        self.eq_enabled = false;
        let Some(headset) = self.output_sink.clone() else {
            // No headset = sinks gone too. Just clear flag and any
            // stale insertions defensively.
            if let Some(ch) = self.game.as_mut() {
                ch.eq = None;
            }
            if let Some(ch) = self.chat.as_mut() {
                ch.eq = None;
            }
            return false;
        };
        if let Some(ch) = self.game.as_mut() {
            let _ = remove_eq_from_channel(ch, GAME_SINK, &headset);
        }
        if let Some(ch) = self.chat.as_mut() {
            let _ = remove_eq_from_channel(ch, CHAT_SINK, &headset);
        }
        info!("EQ disabled (Game + Chat reverted to direct routing)");
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
        // Descriptions cannot contain spaces — pactl's proplist parser
        // splits sink_properties tokens on whitespace with no quote or
        // escape handling, so "Steel Game" would truncate to "Steel".
        // Matching the sink name (no separator) also avoids cognitive
        // mismatch with `pactl list short sinks` output.
        self.game = create_sink_pair(output_sink, GAME_SINK, "SteelGame");
        self.chat = create_sink_pair(output_sink, CHAT_SINK, "SteelChat");
        // Media sink mirrors Game/Chat structurally but is deliberately
        // ignored by the ChatMix dial handler — its volume stays at whatever
        // KDE/pactl set. Use case: music and browser audio that shouldn't
        // dip when the user biases the dial toward chat.
        if self.media_enabled {
            self.media = create_sink_pair(output_sink, MEDIA_SINK, "SteelMedia");
        }
        // HDMI loopback target is independent of the headset — it goes to the
        // host-side HDMI sink (TV / AVR / monitor speakers). Detect at create
        // time so toggling the headset doesn't lose the HDMI route.
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
            info!("Created sinks: {}", active.join(", "));

            // If EQ was on before disconnect (or set via persisted state),
            // re-insert the filter chains now that the sinks exist again.
            if self.eq_enabled {
                let headset = output_sink.to_string();
                if let Some(ch) = self.game.as_mut() {
                    insert_eq_into_channel(
                        ch,
                        GAME_SINK,
                        EQ_GAME_SINK,
                        "SteelVoiceMix Game EQ",
                        &headset,
                    );
                }
                if let Some(ch) = self.chat.as_mut() {
                    insert_eq_into_channel(
                        ch,
                        CHAT_SINK,
                        EQ_CHAT_SINK,
                        "SteelVoiceMix Chat EQ",
                        &headset,
                    );
                }
            }
        } else {
            error!("Failed to create one or more sinks");
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
) -> bool {
    if channel.eq.is_some() {
        return true;
    }

    let spec = FilterChainSpec {
        sink_name: eq_sink_name,
        description: eq_description,
        playback_target: headset,
    };
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
        &format!("sink={eq_sink_name}"),
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
