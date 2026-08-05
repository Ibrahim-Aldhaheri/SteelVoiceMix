//! Virtual surround via PipeWire's `module-filter-chain` convolver.
//!
//! Architecture: a 7.1 null-sink (`SteelSurround`) feeds a filter chain
//! that convolves each directional channel (FL/FR/FC/RL/RR/SL/SR) with
//! a pair of HRIR slices (one per ear) read from a HeSuVi-style 14-
//! channel WAV. LFE bypasses the HRIR and mixes equally into both ears.
//! Output is stereo binaural that the user routes to the headset.
//!
//! ```text
//! SteelSurround (7.1) ─► filter_chain ─► headset (stereo)
//!     │                       │
//!     │                       ├── FL  → copy ─┬→ conv@ch0 (FL→L) ─┐
//!     │                       │               └→ conv@ch1 (FL→R) ─┤
//!     │                       │   …repeat for FR/FC/RL/RR/SL/SR…  │
//!     │                       └── LFE → copy ─→ both mixers       ├─ mix_L
//!     │                                                           └─ mix_R
//! ```
//!
//! ## HRIR file expectations
//!
//! HeSuVi's actual 14-channel WAV layout — verified against the
//! reference convolver config in
//! `loteran/Arctis-Sound-Manager:scripts/pipewire/sink-virtual-surround-7.1-hesuvi.conf`.
//! The layout is NOT a simple "L then R" pair-by-pair pattern past
//! channel 6: the L/R pairs flip starting at FR. Treating it as a
//! regular alternation (which an earlier rev of this file did) made
//! FR-source audio leak into the left ear via FC's impulse, which
//! sounded like a left-bias on stereo content.
//!
//! ```text
//!  0: FL→L    1: FL→R
//!  2: SL→L    3: SL→R
//!  4: RL→L    5: RL→R
//!  6: FC→L          (also reused for LFE→L)
//!  7: FR→R
//!  8: FR→L
//!  9: SR→R
//! 10: SR→L
//! 11: RR→R
//! 12: RR→L
//! 13: FC→R          (also reused for LFE→R)
//! ```
//!
//! LFE bypassed HRIR entirely in the previous rev. We now run it
//! through FC's L/R impulses (separate convolver nodes) — same trick
//! ASM uses, since LFE has no positional info and treating it as
//! front-center is the natural fallback.
//!
//! ## Why a separate module from `filter_chain.rs`
//!
//! Same spawn-pipewire-process pattern but the graph topology and the
//! parameter set (HRIR file path vs per-band biquad params) are different
//! enough that a shared abstraction would be more obscure than helpful.
//! Both modules use the same managed-child-process discipline, so
//! lifecycle is identical.

use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};

use log::{info, warn};

/// Names of the convolver+mixer chain. The capture sink we expose to
/// the rest of PipeWire is named `effect_input.SteelSurround`, mirroring
/// the EQ chain's `effect_*` naming convention.
const CHAIN_NAME: &str = "SteelSurround";

/// Channel keys in the fixed 7.1 order used everywhere in this module
/// and by the GUI stage editor.
pub const SURROUND_CHANNELS: [&str; 8] =
    ["fl", "fr", "fc", "lfe", "rl", "rr", "sl", "sr"];

/// Per-channel level state for the surround stage editor.
///
/// This is the ONLY thing the stage editor changes about the audio:
/// each channel's LEVEL (a gain node baked into the chain). Direction
/// is fixed by the HRIR and is deliberately absent here — see
/// STATUS.md. A change respawns the chain, the same way an EQ band
/// change respawns its channel chain.
#[derive(Clone, Debug)]
pub struct SurroundGains {
    /// dB per channel, in SURROUND_CHANNELS order. Clamped to
    /// [-24, +6] on the way in.
    db: [f32; 8],
    muted: [bool; 8],
    /// When Some, only that channel is audible (monitoring aid). Index
    /// into SURROUND_CHANNELS.
    solo: Option<usize>,
}

impl Default for SurroundGains {
    fn default() -> Self {
        SurroundGains {
            db: [0.0; 8],
            muted: [false; 8],
            solo: None,
        }
    }
}

impl SurroundGains {
    pub fn index_of(key: &str) -> Option<usize> {
        SURROUND_CHANNELS.iter().position(|&k| k == key)
    }

    /// Set a channel's level in dB, clamped to the editable range.
    /// Returns false if the key is unknown.
    pub fn set_gain(&mut self, key: &str, db: f32) -> bool {
        match Self::index_of(key) {
            Some(i) => {
                self.db[i] = db.clamp(-24.0, 6.0);
                true
            }
            None => false,
        }
    }

    pub fn set_mute(&mut self, key: &str, muted: bool) -> bool {
        match Self::index_of(key) {
            Some(i) => {
                self.muted[i] = muted;
                true
            }
            None => false,
        }
    }

    /// Set the solo channel (None clears). Unknown key clears solo.
    pub fn set_solo(&mut self, key: Option<&str>) {
        self.solo = key.and_then(Self::index_of);
    }

    /// Effective linear amplitude multiplier for a channel, accounting
    /// for mute and solo. 0.0 when silenced.
    pub fn linear(&self, i: usize) -> f32 {
        if self.muted[i] {
            return 0.0;
        }
        if let Some(s) = self.solo {
            if s != i {
                return 0.0;
            }
        }
        10.0_f32.powf(self.db[i] / 20.0)
    }
}

/// Where the spawned pipewire child's config file lives.
fn conf_dir() -> Option<PathBuf> {
    let base = std::env::var_os("XDG_RUNTIME_DIR").map(PathBuf::from)?;
    let dir = base.join("steelvoicemix").join("filter-chains");
    fs::create_dir_all(&dir).ok()?;
    Some(dir)
}

/// Spec for a surround chain instance.
pub struct SurroundChainSpec<'a> {
    /// Absolute path to the user's HeSuVi-format HRIR WAV. We don't
    /// validate the channel count up front — PipeWire will refuse to
    /// load the chain if the file is malformed, and the daemon's child
    /// will exit; that's a clear-enough failure mode.
    pub hrir_path: &'a Path,
    /// Downstream node name to link the binaural stereo output to —
    /// typically the headset sink. We rely on an explicit `pw-link`
    /// after spawn rather than `node.target` for the same reason as
    /// the EQ chain (WirePlumber occasionally adds an extra link to
    /// the default sink, creating a feedback loop with EasyEffects).
    pub playback_target: &'a str,
    /// Per-channel level trims from the stage editor.
    pub gains: &'a SurroundGains,
}

/// Live surround chain instance. Drop or `shutdown()` to tear it down.
pub struct SurroundChainHandle {
    child: Child,
    conf_path: PathBuf,
}

impl SurroundChainHandle {
    pub fn spawn(spec: &SurroundChainSpec) -> Option<Self> {
        let dir = conf_dir()?;
        let conf_path = dir.join(format!("{CHAIN_NAME}.conf"));
        let conf = render_conf(spec.hrir_path, spec.gains);
        if let Err(e) = write_conf(&conf_path, &conf) {
            warn!(
                "Failed to write surround conf {}: {e}",
                conf_path.display()
            );
            return None;
        }

        let child = Command::new("pipewire")
            .arg("-c")
            .arg(&conf_path)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|e| warn!("Failed to spawn pipewire for surround: {e}"))
            .ok()?;
        info!(
            "Spawned surround chain pid {} (conf: {}, hrir: {})",
            child.id(),
            conf_path.display(),
            spec.hrir_path.display(),
        );

        // Wait for the spawned pipewire child to register its
        // capture + playback ports, then explicitly link the
        // playback side to the headset. The previous rev used a
        // single 500 ms sleep + single pw-link attempt; on a cold
        // boot wireplumber sometimes takes a couple of seconds to
        // finish enumerating new nodes, so the link silently failed
        // and the user had to toggle surround off→on to recover.
        // Retry with backoff (~500 ms × 8 attempts = up to 4 s) so
        // we cover that window without a wasteful fixed long sleep.
        // Both channels are retried in ONE loop rather than a full retry
        // loop per channel. The old shape paid the settling delay twice
        // (FR re-slept 500 ms even though the node was demonstrably up,
        // FL having just linked), which doubled the cost of every
        // respawn — and a level tweak in the stage editor respawns.
        // First delay is short, then it backs off, so the common case
        // (~150 ms) is fast and a cold-boot WirePlumber that takes
        // seconds to enumerate is still covered (~4 s total).
        let playback_node = format!("effect_output.{CHAIN_NAME}");
        let mut pending: Vec<(String, String)> = ["FL", "FR"]
            .iter()
            .map(|ch| {
                (
                    format!("{playback_node}:output_{ch}"),
                    format!("{}:playback_{ch}", spec.playback_target),
                )
            })
            .collect();
        let mut last_err = String::new();
        for attempt in 0..9 {
            let delay = match attempt {
                0 => 150,
                1 => 250,
                _ => 500,
            };
            std::thread::sleep(std::time::Duration::from_millis(delay));
            pending.retain(|(from, to)| {
                let res = Command::new("pw-link")
                    .args([from, to])
                    .stderr(Stdio::piped())
                    .stdout(Stdio::null())
                    .output();
                match res {
                    Ok(out) if out.status.success() => {
                        info!("Linked {from} → {to} (attempt {})", attempt + 1);
                        false
                    }
                    Ok(out) => {
                        last_err =
                            String::from_utf8_lossy(&out.stderr).trim().to_string();
                        true
                    }
                    Err(e) => {
                        last_err = e.to_string();
                        true
                    }
                }
            });
            if pending.is_empty() {
                break;
            }
        }
        for (from, to) in &pending {
            warn!("pw-link {from} → {to} failed after retries: {last_err}");
        }

        Some(SurroundChainHandle { child, conf_path })
    }

    pub fn shutdown(mut self) {
        let pid = self.child.id();
        let _ = self.child.kill();
        let _ = self.child.wait();
        let _ = fs::remove_file(&self.conf_path);
        info!("Surround chain (pid {pid}) shut down");
    }
}

impl Drop for SurroundChainHandle {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
        let _ = fs::remove_file(&self.conf_path);
    }
}

fn write_conf(path: &Path, contents: &str) -> std::io::Result<()> {
    let mut f = fs::File::create(path)?;
    f.write_all(contents.as_bytes())?;
    f.sync_all()?;
    Ok(())
}

/// Render the full pipewire daemon config for the surround chain.
/// Topology: for each input channel (FL/FR/FC/LFE/RL/RR/SL/SR) a
/// `copy` node fans the signal out to two `convolver` nodes — one
/// per ear, each loading the matching slice from the HeSuVi WAV.
/// All L-ear convolvers feed `mix_l`; all R-ear convolvers feed
/// `mix_r`. Final stereo output flows out via the two mixers.
///
/// LFE is treated as a centre channel: its convolvers reuse the FC
/// HRIR slices (channels 6 and 13). They're declared as separate
/// nodes so the input fan-out stays one-to-one.
fn render_conf(hrir: &Path, gains: &SurroundGains) -> String {
    let hrir_str = hrir.display().to_string();

    // Convolver nodes — (node-name, HRIR-channel-index). Layout is
    // verbatim from ASM's reference config; deviating from this
    // (e.g. assuming alternating L/R pairs all the way down)
    // produces the audible left-bias bug.
    let convolvers: &[(&str, u32)] = &[
        ("fl_l", 0),
        ("fl_r", 1),
        ("sl_l", 2),
        ("sl_r", 3),
        ("rl_l", 4),
        ("rl_r", 5),
        ("fc_l", 6),
        ("fr_r", 7),
        ("fr_l", 8),
        ("sr_r", 9),
        ("sr_l", 10),
        ("rr_r", 11),
        ("rr_l", 12),
        ("fc_r", 13),
        // LFE reuses FC's L/R impulses — separate nodes so each
        // input port has exactly one downstream convolver per ear.
        ("lfe_l", 6),
        ("lfe_r", 13),
    ];

    let mut nodes: Vec<String> = Vec::new();
    // One `copy` per input channel — a single fan-out point into the
    // two ear convolvers. The per-channel LEVEL trim from the stage
    // editor is applied by the convolver's own `gain` config parameter
    // (see below), NOT by a separate node.
    for key in SURROUND_CHANNELS {
        nodes.push(format!(
            r#"                    {{ type = builtin name = {key}_copy label = copy }}"#,
        ));
    }
    for (name, channel) in convolvers {
        // The convolver's `gain` config parameter scales the impulse
        // response — a documented, config-time value applied when the IR
        // loads, so it does NOT depend on a control-port init (an
        // earlier `linear` gain node relied on the `Mult` control port,
        // which did not attenuate on hardware). gain = 0.0 when the
        // channel is muted or soloed away. Both ear convolvers for a
        // channel share the channel's gain, so the L/R balance from the
        // HRIR is preserved.
        //
        // blocksize=512 pins the FFT partition size so the convolver
        // doesn't auto-replan when the graph's quantum shifts (KDE
        // volume slider / wine / Discord can yank the global quantum to
        // 32, and a 14-instance re-plan is a major glitch source — see
        // PipeWire bug #4013 and EasyEffects #1567). tailsize is left
        // default so the convolver uses the natural IR length.
        let key = name.rsplit_once('_').map(|(p, _)| p).unwrap_or(name);
        let mult = SurroundGains::index_of(key)
            .map(|i| gains.linear(i))
            .unwrap_or(1.0);
        nodes.push(format!(
            r#"                    {{
                        type  = builtin
                        name  = {name}
                        label = convolver
                        config = {{ filename = "{hrir}" channel = {channel} blocksize = 512 gain = {mult:.5} }}
                    }}"#,
            hrir = hrir_str,
        ));
    }
    nodes.push(
        r#"                    { type = builtin name = mix_l label = mixer }"#.to_string(),
    );
    nodes.push(
        r#"                    { type = builtin name = mix_r label = mixer }"#.to_string(),
    );

    // Link list: input fan-out, then convolver → mixer.
    // Pairs are (input-channel-key, convolver-prefix) — each input
    // fans into a `<prefix>_l` and `<prefix>_r` convolver. LFE goes
    // to its own LFE convolvers (which themselves reuse FC's HRIR
    // channels), so the topology stays one-to-one.
    let fan_out: &[(&str, &str)] = &[
        ("fl", "fl"),
        ("fr", "fr"),
        ("fc", "fc"),
        ("lfe", "lfe"),
        ("rl", "rl"),
        ("rr", "rr"),
        ("sl", "sl"),
        ("sr", "sr"),
    ];

    let mut links: Vec<String> = Vec::new();
    for (input_key, conv_prefix) in fan_out {
        // copy → both ear convolvers (level is baked into the
        // convolver's gain config, so there's no intermediate node).
        links.push(format!(
            r#"                    {{ output = "{input_key}_copy:Out"  input = "{conv_prefix}_l:In" }}"#,
        ));
        links.push(format!(
            r#"                    {{ output = "{input_key}_copy:Out"  input = "{conv_prefix}_r:In" }}"#,
        ));
    }
    // Convolver output → matching ear's mixer. Port numbers are
    // 1-based and just an ordering — they don't affect mixing
    // semantics, but PipeWire wants distinct ones per source.
    for (i, (_, conv_prefix)) in fan_out.iter().enumerate() {
        let port = i + 1;
        links.push(format!(
            r#"                    {{ output = "{conv_prefix}_l:Out"  input = "mix_l:In {port}" }}"#,
        ));
        links.push(format!(
            r#"                    {{ output = "{conv_prefix}_r:Out"  input = "mix_r:In {port}" }}"#,
        ));
    }

    // External-port mapping: PipeWire's audio.position list defines the
    // 7.1 channel order; the inputs array specifies which internal port
    // each external channel feeds. Order MUST match audio.position
    // below.
    let inputs = [
        "fl_copy:In",
        "fr_copy:In",
        "fc_copy:In",
        "lfe_copy:In",
        "rl_copy:In",
        "rr_copy:In",
        "sl_copy:In",
        "sr_copy:In",
    ]
    .iter()
    .map(|p| format!(r#""{p}""#))
    .collect::<Vec<_>>()
    .join(" ");

    let nodes_block = nodes.join("\n");
    let links_block = links.join("\n");

    format!(
        r#"context.properties = {{
    log.level = 0
    core.daemon = false
    core.name = steelvoicemix-surround
}}

context.modules = [
    {{ name = libpipewire-module-rt
        args = {{
            nice.level = -11
            rt.prio = 88
            rt.time.soft = 200000
            rt.time.hard = 200000
        }}
        flags = [ ifexists nofail ]
    }}
    {{ name = libpipewire-module-protocol-native }}
    {{ name = libpipewire-module-client-node }}
    {{ name = libpipewire-module-adapter }}
    {{ name = libpipewire-module-metadata }}

    {{ name = libpipewire-module-filter-chain
        flags = [ nofail ]
        args = {{
            node.description = "SteelVoiceMix Surround"
            media.name       = "SteelVoiceMix Surround"
            filter.graph = {{
                nodes = [
{nodes_block}
                ]
                links = [
{links_block}
                ]
                inputs  = [ {inputs} ]
                outputs = [ "mix_l:Out" "mix_r:Out" ]
            }}
            audio.channels = 8
            audio.position = [ FL FR FC LFE RL RR SL SR ]
            capture.props = {{
                node.name        = "effect_input.{name}"
                node.description = "SteelSurround"
                media.class      = Audio/Sink
                audio.channels   = 8
                audio.position   = [ FL FR FC LFE RL RR SL SR ]
                # node.lock-quantum pins this filter chain's buffer
                # size so a downstream client (Discord, KDE volume
                # OSD, OBS, wine) requesting low-latency capture
                # can't drag the convolver's quantum down to 32 and
                # trigger a 14-instance FFT re-plan storm. The
                # graph still negotiates rate normally.
                node.lock-quantum    = true
                node.latency         = 1024/48000
                # node.suspend-on-idle = false keeps the chain alive
                # while no audio is flowing. PipeWire's default
                # behavior suspends idle nodes; on resume (game
                # refocus, browser tab switching back to a video,
                # any silence-then-audio transition) the node wake-
                # up costs ~1 ms and is audible as a brief glitch.
                # Cost of staying alive: a few KB of RAM and zero
                # CPU on a chain with no input. Worth it.
                node.suspend-on-idle = false
            }}
            playback.props = {{
                node.name            = "effect_output.{name}"
                node.passive         = true
                node.autoconnect     = false
                node.dont-reconnect  = true
                audio.channels       = 2
                audio.position       = [ FL FR ]
                node.lock-quantum    = true
                node.latency         = 1024/48000
                node.suspend-on-idle = false
            }}
        }}
    }}
]
"#,
        name = CHAIN_NAME,
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_gains_are_unity() {
        let g = SurroundGains::default();
        for i in 0..8 {
            assert!((g.linear(i) - 1.0).abs() < 1e-6);
        }
    }

    #[test]
    fn gain_is_clamped_to_range() {
        let mut g = SurroundGains::default();
        assert!(g.set_gain("fl", 999.0));
        // +6 dB max ≈ 1.995 linear.
        assert!(g.linear(0) < 2.01);
        assert!(g.set_gain("fl", -999.0));
        // -24 dB min ≈ 0.063 linear.
        assert!(g.linear(0) < 0.07 && g.linear(0) > 0.0);
    }

    #[test]
    fn mute_silences_only_that_channel() {
        let mut g = SurroundGains::default();
        assert!(g.set_mute("fc", true));
        let fc = SurroundGains::index_of("fc").unwrap();
        assert_eq!(g.linear(fc), 0.0);
        let fl = SurroundGains::index_of("fl").unwrap();
        assert!((g.linear(fl) - 1.0).abs() < 1e-6);
    }

    #[test]
    fn solo_silences_all_but_one() {
        let mut g = SurroundGains::default();
        g.set_solo(Some("sr"));
        let sr = SurroundGains::index_of("sr").unwrap();
        for i in 0..8 {
            if i == sr {
                assert!(g.linear(i) > 0.0);
            } else {
                assert_eq!(g.linear(i), 0.0);
            }
        }
        g.set_solo(None);
        for i in 0..8 {
            assert!(g.linear(i) > 0.0);
        }
    }

    #[test]
    fn unknown_channel_is_rejected() {
        let mut g = SurroundGains::default();
        assert!(!g.set_gain("nope", 0.0));
        assert!(!g.set_mute("nope", true));
        assert!(SurroundGains::index_of("nope").is_none());
    }

    #[test]
    fn muted_channel_gets_zero_convolver_gain() {
        // Level is applied via the convolver's `gain` config parameter
        // (config-time, no control-port dependency). A muted channel
        // must render gain = 0 on BOTH of its ear convolvers.
        let mut g = SurroundGains::default();
        g.set_mute("fl", true);
        let conf = render_conf(std::path::Path::new("/tmp/x.wav"), &g);
        for node in ["fl_l", "fl_r"] {
            // Find the convolver block for this node and confirm gain=0.
            let idx = conf.find(&format!("name  = {node}\n")).unwrap_or_else(|| {
                panic!("convolver {node} not found")
            });
            let block = &conf[idx..idx + 220];
            assert!(
                block.contains("gain = 0.00000"),
                "{node} should have gain 0 when muted; got: {block}"
            );
        }
        // An un-muted channel stays at unity.
        let fr_idx = conf.find("name  = fr_l\n").unwrap();
        assert!(conf[fr_idx..fr_idx + 220].contains("gain = 1.00000"));
    }

    #[test]
    fn gain_uses_convolver_config_not_a_linear_node() {
        // Regression guard against the reverted approach: no separate
        // `_gain` linear nodes, and the level lives in the convolver
        // config's `gain =` field.
        let mut g = SurroundGains::default();
        g.set_gain("sr", -6.0);
        let conf = render_conf(std::path::Path::new("/tmp/x.wav"), &g);
        assert!(!conf.contains("label = linear"), "no linear gain nodes");
        assert!(!conf.contains("_gain:In"), "no gain-node links");
        assert!(conf.contains("gain = "), "convolver gain not rendered");
        // sr at -6 dB ≈ 0.501 on its convolvers.
        let idx = conf.find("name  = sr_l\n").unwrap();
        assert!(conf[idx..idx + 220].contains("gain = 0.50119"));
    }
}


