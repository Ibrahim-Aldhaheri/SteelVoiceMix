//! Microphone capture-path filter chain via `module-filter-chain` +
//! the `pipewire -c <conf>` spawned-child pattern.
//!
//! Architecture is the mirror image of `surround_chain.rs`:
//! the chain captures from the hardware Arctis mic, applies whichever
//! processing nodes the user has enabled, and exposes a virtual
//! `SteelMic` source that apps record from. Audio flows
//!
//! ```text
//! hardware mic ──► capture (Audio/Sink invisible) ─┐
//!                                                  ▼
//!     [optional gate_1410] → [optional RNNoise] → mixer/copy
//!                                                  │
//!                                                  ▼
//!     playback (Audio/Source "SteelMic", visible to apps)
//! ```
//!
//! Two LADSPA plugins are used. Both are standard Fedora packages but
//! we don't hard-require them — if missing, the spawned pipewire
//! child fails to load the module and the daemon logs a warning. The
//! chain just doesn't come up; the user still has the bare hardware
//! mic.
//!
//! - `gate_1410` from **swh-plugins** (Steve Harris) — simple
//!   threshold-based noise gate.
//! - `librnnoise_ladspa` from **noise-suppression-for-voice** — the
//!   RNNoise neural denoiser, used for both "Noise Reduction" (mild)
//!   and "AI Noise Cancellation" (aggressive). When both are enabled
//!   only the AI-NC stage runs (running RNNoise twice in series adds
//!   latency without meaningful benefit).
//!
//! ## Strength → control mapping
//!
//! Strength is a UI-friendly 0..=100 scale. Each filter gets it
//! converted into the parameter the plugin actually wants:
//!
//! - Gate threshold dB: `-60 + strength * 0.6` (0 → -60 dB, 100 → 0 dB).
//! - NR VAD threshold (%):   `strength * 0.5`  (mild — capped at 50%).
//! - AI-NC VAD threshold (%): `strength * 0.95` (aggressive — up to 95%).

use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};

use log::{info, warn};

use crate::protocol::{BandType, EqBand, MicState, VolumeStabilizerKind, NUM_BANDS};

/// Name suffix the chain uses for its capture-side virtual source.
/// Apps see `SteelMic` as their input device.
pub const CHAIN_NAME: &str = "SteelMic";

fn conf_dir() -> Option<PathBuf> {
    let base = std::env::var_os("XDG_RUNTIME_DIR").map(PathBuf::from)?;
    let dir = base.join("steelvoicemix").join("filter-chains");
    fs::create_dir_all(&dir).ok()?;
    Some(dir)
}

/// Spec for a microphone chain instance.
pub struct MicChainSpec<'a> {
    /// Hardware microphone source the chain captures from. Set via
    /// `node.target` on the chain's capture side so it doesn't drift
    /// to whatever the system default mic happens to be.
    pub mic_source: &'a str,
    /// Which features are on + their strengths.
    pub state: MicState,
    /// Per-band EQ applied between the gate / RNNoise stages and the
    /// chain output. Always rendered into the conf when the chain
    /// spawns (PipeWire biquads are cheap; flat bands are a no-op).
    /// The same shape as our output-channel EQ.
    pub eq_bands: [EqBand; NUM_BANDS],
}

impl<'a> MicChainSpec<'a> {
    /// True when at least one feature is enabled OR any EQ band has
    /// a non-zero gain. Without one of these the chain would be pure
    /// passthrough and we shouldn't bother spawning a separate
    /// PipeWire process.
    pub fn has_active_features(&self) -> bool {
        if self.state.noise_gate.enabled
            || self.state.noise_reduction.enabled
            || self.state.ai_noise_cancellation.enabled
            || self.state.volume_stabilizer.enabled
        {
            return true;
        }
        self.eq_bands.iter().any(|b| b.enabled && b.gain.abs() > 0.05)
    }
}

/// Live mic chain instance. Drop or `shutdown()` to tear it down.
pub struct MicChainHandle {
    child: Child,
    conf_path: PathBuf,
}

impl MicChainHandle {
    pub fn spawn(spec: &MicChainSpec) -> Option<Self> {
        if !spec.has_active_features() {
            return None;
        }
        let dir = conf_dir()?;
        let conf_path = dir.join(format!("{CHAIN_NAME}.conf"));
        let conf = render_conf(spec);
        if let Err(e) = write_conf(&conf_path, &conf) {
            warn!(
                "Failed to write mic conf {}: {e}",
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
            .map_err(|e| warn!("Failed to spawn pipewire for mic: {e}"))
            .ok()?;
        info!(
            "Spawned mic chain pid {} (conf: {}, source: {})",
            child.id(),
            conf_path.display(),
            spec.mic_source,
        );

        Some(MicChainHandle { child, conf_path })
    }

    pub fn shutdown(mut self) {
        let pid = self.child.id();
        let _ = self.child.kill();
        let _ = self.child.wait();
        let _ = fs::remove_file(&self.conf_path);
        info!("Mic chain (pid {pid}) shut down");
    }

    /// True if the spawned `pipewire -c <conf>` child has exited.
    /// Returns true on transient errors too — those almost always
    /// mean the child is gone and the watchdog should respawn anyway.
    /// The hardware ALSA source disappearing during system suspend
    /// kills the child without us noticing; the watchdog uses this
    /// to detect that and trigger a fresh spawn.
    pub fn is_dead(&mut self) -> bool {
        match self.child.try_wait() {
            Ok(Some(_)) => true,
            Ok(None) => false,
            Err(_) => true,
        }
    }
}

impl Drop for MicChainHandle {
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

fn gate_threshold_db(strength: u8) -> f32 {
    // 0 → -60 dB (effectively never gates), 100 → 0 dB (cuts most signal).
    -60.0 + f32::from(strength).clamp(0.0, 100.0) * 0.6
}

fn rnnoise_vad_pct(strength: u8, max: f32) -> f32 {
    f32::from(strength).clamp(0.0, 100.0) * (max / 100.0)
}

fn mic_band_label(t: BandType) -> &'static str {
    match t {
        BandType::Lowshelf => "bq_lowshelf",
        BandType::Peaking => "bq_peaking",
        BandType::Highshelf => "bq_highshelf",
        BandType::Lowpass => "bq_lowpass",
        BandType::Highpass => "bq_highpass",
        BandType::Bandpass => "bq_bandpass",
        BandType::Notch => "bq_notch",
        BandType::Allpass => "bq_allpass",
    }
}

fn render_conf(spec: &MicChainSpec) -> String {
    // Build the node + link lists conditionally. Each enabled feature
    // adds one node; the chain ends with a `copy` so the playback
    // side always has something to attach to even if no LADSPA plugin
    // loaded successfully (graceful degradation: the user still gets
    // a SteelMic source, just unprocessed).
    let s = spec.state;

    // Decide which RNNoise stage to run. AI-NC takes precedence over
    // mild NR — running both in series adds latency without benefit.
    let rnnoise_enabled = s.noise_reduction.enabled || s.ai_noise_cancellation.enabled;
    let rnnoise_vad = if s.ai_noise_cancellation.enabled {
        rnnoise_vad_pct(s.ai_noise_cancellation.strength, 95.0)
    } else {
        rnnoise_vad_pct(s.noise_reduction.strength, 50.0)
    };

    // Walk the chain in order: gate → rnnoise → terminator. Each
    // emit-step records the previous node's output port so the next
    // step's input link can target it.
    let mut nodes: Vec<String> = Vec::new();
    let mut links: Vec<String> = Vec::new();
    let mut last_out: Option<&str> = None;

    if s.noise_gate.enabled {
        let threshold = gate_threshold_db(s.noise_gate.strength);
        nodes.push(format!(
            r#"                    {{
                        type   = ladspa
                        name   = mic_gate
                        plugin = "gate_1410"
                        label  = gate
                        control = {{
                            "LF key filter (Hz)" = 100.0
                            "HF key filter (Hz)" = 6000.0
                            "Threshold (dB)" = {threshold:.2}
                            "Attack (ms)" = 1.0
                            "Hold (ms)" = 50.0
                            "Decay (ms)" = 100.0
                            "Range (dB)" = -90.0
                        }}
                    }}"#,
        ));
        if let Some(prev) = last_out {
            links.push(format!(
                r#"                    {{ output = "{prev}"  input = "mic_gate:Input" }}"#,
            ));
        }
        last_out = Some("mic_gate:Output");
    }

    if rnnoise_enabled {
        nodes.push(format!(
            r#"                    {{
                        type   = ladspa
                        name   = mic_rnnoise
                        plugin = "librnnoise_ladspa"
                        label  = noise_suppressor_mono
                        control = {{ "VAD Threshold (%)" = {rnnoise_vad:.2} }}
                    }}"#,
        ));
        if let Some(prev) = last_out {
            links.push(format!(
                r#"                    {{ output = "{prev}"  input = "mic_rnnoise:Input" }}"#,
            ));
        }
        last_out = Some("mic_rnnoise:Output");
    }

    // Volume Stabilizer — pluggable LADSPA compressor selected by
    // s.volume_stabilizer_kind. Both options ship in
    // ladspa-swh-plugins (Steve Harris, GPL).
    //
    //   Broadcast → SC4 mono (sc4m_1916). Audibly levels loud /
    //   quiet swings via threshold + ratio + makeup gain. Default.
    //
    //   Soft → Dyson (dyson_compress_1403). Older, gentler. Kept
    //   for users who want a transparent option.
    if s.volume_stabilizer.enabled {
        let strength = f32::from(s.volume_stabilizer.strength).clamp(0.0, 100.0);
        let stage_node = match s.volume_stabilizer_kind {
            VolumeStabilizerKind::Broadcast => {
                let threshold_db = -strength * 0.30;
                let ratio = 1.0 + strength * 0.07;
                let makeup_db = strength * 0.06;
                format!(
                    r#"                    {{
                        type   = ladspa
                        name   = mic_stabilizer
                        plugin = "sc4m_1916"
                        label  = sc4m
                        control = {{
                            "RMS/peak" = 0.0
                            "Attack time (ms)" = 5.0
                            "Release time (ms)" = 50.0
                            "Threshold level (dB)" = {threshold_db:.2}
                            "Ratio (1:n)" = {ratio:.2}
                            "Knee radius (dB)" = 4.0
                            "Makeup gain (dB)" = {makeup_db:.2}
                        }}
                    }}"#,
                )
            }
            VolumeStabilizerKind::Soft => {
                let comp_ratio = strength * 0.0085;
                format!(
                    r#"                    {{
                        type   = ladspa
                        name   = mic_stabilizer
                        plugin = "dyson_compress_1403"
                        label  = dysonCompress
                        control = {{
                            "Peak limit (dB)" = -3.0
                            "Release time (s)" = 0.10
                            "Fast compression ratio" = 0.50
                            "Compression ratio" = {comp_ratio:.4}
                        }}
                    }}"#,
                )
            }
        };
        nodes.push(stage_node);
        if let Some(prev) = last_out {
            links.push(format!(
                r#"                    {{ output = "{prev}"  input = "mic_stabilizer:Input" }}"#,
            ));
        }
        last_out = Some("mic_stabilizer:Output");
    }

    // Mic-side parametric EQ — 10 biquad stages chained in series.
    // Same builtin biquad nodes the output EQ uses (filter_chain.rs's
    // band_label + render_band_node patterns). Always emitted when
    // the chain spawns; flat bands cost ~10 us per stage.
    for (i, band) in spec.eq_bands.iter().enumerate() {
        let idx = i + 1;
        let gain = if band.enabled { band.gain } else { 0.0 };
        let q = band.q.max(0.0001);
        let label = mic_band_label(band.band_type);
        nodes.push(format!(
            r#"                    {{
                        type  = builtin
                        name  = mic_eq_{idx}
                        label = {label}
                        control = {{ "Freq" = {freq:.4}  "Q" = {q:.4}  "Gain" = {gain:.4} }}
                    }}"#,
            idx = idx,
            label = label,
            freq = band.freq,
            q = q,
            gain = gain,
        ));
        if let Some(prev) = last_out {
            links.push(format!(
                r#"                    {{ output = "{prev}"  input = "mic_eq_{idx}:In" }}"#,
                idx = idx,
                prev = prev,
            ));
        }
        // Each iteration's owned String would die at end of scope, so
        // we leak via Box::leak to keep `last_out: Option<&str>`. The
        // chain is rendered once per spawn — the few tiny leaks per
        // build are reclaimed when the daemon exits.
        let owned: &'static str = Box::leak(format!("mic_eq_{idx}:Out").into_boxed_str());
        last_out = Some(owned);
    }

    // Terminator: a `copy` builtin so the chain has a stable output
    // node name regardless of which LADSPA stages were included. Also
    // gives us a safe place to land if a LADSPA plugin failed to load
    // — we still expose SteelMic with a passthrough copy.
    nodes.push(
        r#"                    { type = builtin name = mic_out label = copy }"#.to_string(),
    );
    if let Some(prev) = last_out {
        links.push(format!(
            r#"                    {{ output = "{prev}"  input = "mic_out:In" }}"#,
        ));
    }

    let nodes_block = nodes.join("\n");
    let links_block = if links.is_empty() {
        // Single-node chain: graph has no internal links. Empty array
        // is valid SPA-JSON and keeps the rendered conf well-formed.
        String::new()
    } else {
        links.join("\n")
    };

    // First-stage node receives the audio from the chain's external
    // input port. Walk the chain order (gate → rnnoise → stabilizer
    // → eq) and pick whichever stage is the earliest enabled one.
    let first_input_port = if s.noise_gate.enabled {
        "mic_gate:Input"
    } else if rnnoise_enabled {
        "mic_rnnoise:Input"
    } else if s.volume_stabilizer.enabled {
        "mic_stabilizer:Input"
    } else {
        // No gate, no rnnoise, no stabilizer — EQ stages run from
        // band 1. We always emit the 10 biquads when the chain
        // spawns, so this is always reachable when
        // has_active_features is true via EQ.
        "mic_eq_1:In"
    };

    format!(
        r#"context.properties = {{
    log.level = 0
    core.daemon = false
    core.name = steelvoicemix-mic-chain
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
            node.description = "SteelVoiceMix Microphone"
            media.name       = "SteelVoiceMix Microphone"
            audio.rate       = 48000
            filter.graph = {{
                nodes = [
{nodes_block}
                ]
                links = [
{links_block}
                ]
                inputs  = [ "{first_input_port}" ]
                outputs = [ "mic_out:Out" ]
            }}
            capture.props = {{
                # Capture side hooks the hardware mic — node.target
                # pins it explicitly so the chain doesn't follow the
                # system default if the user changes their default
                # source elsewhere.
                node.name        = "capture.{name}"
                node.target      = "{mic_source}"
                node.passive     = true
                audio.channels   = 1
                audio.position   = [ MONO ]
            }}
            playback.props = {{
                node.name        = "{name}"
                node.description = "{name}"
                media.class      = Audio/Source
                audio.channels   = 1
                audio.position   = [ MONO ]
            }}
        }}
    }}
]
"#,
        name = CHAIN_NAME,
        mic_source = spec.mic_source,
    )
}
