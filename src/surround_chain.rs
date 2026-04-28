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
//! HeSuVi's standard 14-channel WAV layout (this is what we assume —
//! third-party HRIRs may follow it but it's not universal):
//!
//! ```text
//!  0: FL→L    1: FL→R     <-- front left source, both ears
//!  2: SL→L    3: SL→R     <-- side left
//!  4: BL→L    5: BL→R     <-- back/rear left
//!  6: FR→L    7: FR→R
//!  8: SR→L    9: SR→R
//! 10: BR→L   11: BR→R
//! 12: FC→L   13: FC→R
//! ```
//!
//! Most HeSuVi presets (Atmos, DTS, GoodHurt, Sonic Studio, etc.) ship
//! in this layout. If a user's HRIR uses a different shape the result
//! will be wrong but won't crash — the daemon doesn't try to be clever.
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
        let conf = render_conf(spec.hrir_path);
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

        // Same 500 ms wait + explicit pw-link as the EQ chain — the
        // chain output's effect_output.SteelSurround:output_FL/FR
        // ports need time to register before we can link them.
        std::thread::sleep(std::time::Duration::from_millis(500));
        let playback_node = format!("effect_output.{CHAIN_NAME}");
        for ch in ["FL", "FR"] {
            let from = format!("{playback_node}:output_{ch}");
            let to = format!("{}:playback_{ch}", spec.playback_target);
            let res = Command::new("pw-link")
                .args([&from, &to])
                .stderr(Stdio::piped())
                .stdout(Stdio::null())
                .output();
            match res {
                Ok(out) if out.status.success() => {
                    info!("Linked {from} → {to}");
                }
                Ok(out) => warn!(
                    "pw-link {from} → {to} failed: {}",
                    String::from_utf8_lossy(&out.stderr).trim()
                ),
                Err(e) => warn!("pw-link {from} → {to} spawn error: {e}"),
            }
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
/// Each directional channel goes through a `copy` node (split fan-out)
/// then into two `convolver` nodes (one per ear), all summed by two
/// final mixer nodes. LFE bypasses the HRIR — most movie LFE tracks
/// are sub-bass that doesn't have a meaningful HRIR signature anyway.
fn render_conf(hrir: &Path) -> String {
    let hrir_str = hrir.display().to_string();
    // Pairs are (channel-key, HeSuVi channel index for L ear, idx for R ear).
    // Order matches the audio.position list below; LFE is omitted because
    // it goes straight through the lfe_copy node into both mixers.
    let directional = [
        ("fl", 0u32, 1u32),
        ("sl", 2, 3),
        ("rl", 4, 5),
        ("fr", 6, 7),
        ("sr", 8, 9),
        ("rr", 10, 11),
        ("fc", 12, 13),
    ];

    // Build node list: for each directional channel, one copy + two
    // convolvers. Plus one copy for LFE, plus two mixers for the
    // stereo output.
    let mut nodes: Vec<String> = Vec::new();
    for (key, ch_l, ch_r) in &directional {
        nodes.push(format!(
            r#"                    {{ type = builtin name = {key}_copy label = copy }}"#,
        ));
        nodes.push(format!(
            r#"                    {{
                        type  = builtin
                        name  = {key}_l
                        label = convolver
                        config = {{ filename = "{hrir}" channel = {ch_l} }}
                    }}"#,
            hrir = hrir_str,
        ));
        nodes.push(format!(
            r#"                    {{
                        type  = builtin
                        name  = {key}_r
                        label = convolver
                        config = {{ filename = "{hrir}" channel = {ch_r} }}
                    }}"#,
            hrir = hrir_str,
        ));
    }
    nodes.push(
        r#"                    { type = builtin name = lfe_copy label = copy }"#.to_string(),
    );
    nodes.push(
        r#"                    { type = builtin name = mix_l label = mixer }"#.to_string(),
    );
    nodes.push(
        r#"                    { type = builtin name = mix_r label = mixer }"#.to_string(),
    );

    // Build link list: each directional copy fans out to its L and R
    // convolvers; each convolver feeds the matching ear's mixer; LFE
    // copy feeds both mixers equally.
    let mut links: Vec<String> = Vec::new();
    for (i, (key, _, _)) in directional.iter().enumerate() {
        links.push(format!(
            r#"                    {{ output = "{key}_copy:Out"  input = "{key}_l:In" }}"#,
        ));
        links.push(format!(
            r#"                    {{ output = "{key}_copy:Out"  input = "{key}_r:In" }}"#,
        ));
        // Mixer port indices are 1-based and dynamically allocated.
        let port = i + 1;
        links.push(format!(
            r#"                    {{ output = "{key}_l:Out"  input = "mix_l:In {port}" }}"#,
        ));
        links.push(format!(
            r#"                    {{ output = "{key}_r:Out"  input = "mix_r:In {port}" }}"#,
        ));
    }
    let lfe_port = directional.len() + 1;
    links.push(format!(
        r#"                    {{ output = "lfe_copy:Out"  input = "mix_l:In {lfe_port}" }}"#,
    ));
    links.push(format!(
        r#"                    {{ output = "lfe_copy:Out"  input = "mix_r:In {lfe_port}" }}"#,
    ));

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
            }}
            playback.props = {{
                node.name           = "effect_output.{name}"
                node.passive        = true
                node.autoconnect    = false
                node.dont-reconnect = true
                audio.channels      = 2
                audio.position      = [ FL FR ]
            }}
        }}
    }}
]
"#,
        name = CHAIN_NAME,
    )
}
