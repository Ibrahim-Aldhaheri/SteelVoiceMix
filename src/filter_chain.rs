//! PipeWire filter-chain integration via a spawned `pipewire -c <conf>` child.
//!
//! The filter chain sits **between** a SteelGame/SteelChat null-sink and the
//! headset, NOT bolted into the sink itself. That distinction is the whole
//! reason this module exists separately from `audio.rs`:
//!
//! ```text
//! EQ off:  SteelGame (null-sink) ──loopback──► headset
//! EQ on:   SteelGame (null-sink) ──loopback──► SteelGameEQ (filter-chain)
//!                                              ──loopback──► headset
//! ```
//!
//! Toggling EQ swaps loopback targets only — the SteelGame null-sink Discord
//! is bound to never goes away. Apps holding sink references stay connected
//! across every settings change. ASM bolted filters into the sink itself
//! and shipped with the resulting "Discord drops on EQ tweak" bug; we
//! deliberately don't.
//!
//! ## Why a child `pipewire` process and not pactl/pw-cli
//!
//! Two dead ends investigated and ruled out:
//!
//! - `pactl load-module module-filter-chain ...` — the pulse-protocol bridge
//!   only proxies pulse-style key=value modules (null-sink, loopback). It
//!   rejects PipeWire-native filter-chain's nested SPA-JSON config with
//!   "No such entity".
//! - `pw-cli load-module libpipewire-module-filter-chain ...` — appears to
//!   succeed (exit 0) but the loaded module is owned by the pw-cli
//!   connection. When pw-cli exits, the module unloads with it. Useless for
//!   persistent toggling.
//!
//! The pattern that survives: write a SPA-JSON config to disk, then run
//! `pipewire -c <conf>` as a long-lived child. That spawned pipewire
//! instance loads the filter chain into the main daemon and stays alive
//! holding the modules. Killing the child unloads the chain.
//!
//! For now the chain is a single passthrough `copy` node per channel —
//! audibly inert, but it proves the wiring. Real biquad EQ bands and the
//! HeSuVi convolver will plug into the same scaffolding without further
//! architectural change.

use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};

use log::{info, warn};

use crate::protocol::{BandType, EqBand, NUM_BANDS};

/// Describes one filter-chain instance the daemon manages.
pub struct FilterChainSpec<'a> {
    /// Logical name suffix — gets prefixed to `effect_input.` for the
    /// capture-side sink (what loopbacks target) and `effect_output.`
    /// for the playback-side stream. The `effect_*` prefix is the
    /// PipeWire convention for internal pipeline nodes; plasma-pa /
    /// pavucontrol filter these out of the user-facing audio device
    /// list, so users don't see internal EQ stages as routable
    /// destinations.
    pub sink_name: &'a str,
    /// Human-readable description shown in plasma-pa / pavucontrol.
    pub description: &'a str,
    /// PipeWire node name of the downstream target — typically the real
    /// headset sink name. The filter chain's playback side binds to this.
    pub playback_target: &'a str,
    /// Full per-band parameters. Frequency, Q, gain (dB) and biquad type
    /// are all driven from the EqBand records, so a preset whose
    /// `parametricEQ.filter1..filter10` entries map into this array
    /// gets reproduced verbatim. Disabled bands collapse to passthrough by
    /// emitting them with gain=0.0 — keeping the chain's node count
    /// identical between presets simplifies the link list.
    pub bands: [EqBand; NUM_BANDS],
}

impl<'a> FilterChainSpec<'a> {
    /// The prefixed sink name a loopback should target to feed audio
    /// into this chain.
    pub fn capture_sink(&self) -> String {
        format!("effect_input.{}", self.sink_name)
    }

    /// The prefixed playback-side node name; ports of the form
    /// `<this>:output_FL` / `:output_FR` are what carry processed audio
    /// out of the chain.
    pub fn playback_node(&self) -> String {
        format!("effect_output.{}", self.sink_name)
    }
}

impl<'a> FilterChainSpec<'a> {
    /// Render the full pipewire daemon config (`context.modules = [ ... ]`)
    /// that, when run via `pipewire -c <file>`, loads exactly this filter
    /// chain into the running PipeWire instance.
    fn to_pipewire_conf(&self) -> String {
        // Filter graph adapted from PipeWire's canonical `sink-eq6.conf`
        // (`/usr/share/pipewire/filter-chain/`), extended to 10 bands so
        // the parametricEQ.filter1..filter10 preset shape maps 1:1.
        //
        // Critical idioms preserved verbatim:
        //
        //   - Single node per band (not per channel). PipeWire auto-
        //     duplicates each node across channels driven by audio.channels.
        //   - Quoted control keys with float literals: "Freq" = 100.0 etc.
        //     Unquoted/integer forms parse but produce silently broken
        //     biquad coefficients.
        //   - No explicit inputs/outputs arrays — derived from links.
        //
        // Disabled bands collapse to gain=0.0 rather than being omitted —
        // keeps the node count and link list constant across presets, so
        // we never have to renumber when a preset switches which bands
        // are active.
        let nodes = self
            .bands
            .iter()
            .enumerate()
            .map(|(i, b)| render_band_node(i + 1, b))
            .collect::<Vec<_>>()
            .join("\n");
        let links = (1..NUM_BANDS)
            .map(|i| {
                format!(
                    r#"                    {{ output = "eq_band_{a}:Out"  input = "eq_band_{b}:In" }}"#,
                    a = i,
                    b = i + 1,
                )
            })
            .collect::<Vec<_>>()
            .join("\n");

        format!(
            r#"context.properties = {{
    log.level = 0
    core.daemon = false
    core.name = steelvoicemix-filter-chain-{name}
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
            node.description = "{desc}"
            media.name       = "{desc}"
            filter.graph = {{
                nodes = [
{nodes}
                ]
                links = [
{links}
                ]
            }}
            audio.channels = 2
            audio.position = [ FL FR ]
            capture.props = {{
                node.name   = "effect_input.{name}"
                media.class = Audio/Sink
            }}
            playback.props = {{
                node.name           = "effect_output.{name}"
                node.passive        = true
                # All three flags are needed when the user has a non-headset
                # default sink (EasyEffects, etc.):
                #   - node.target is DELIBERATELY OMITTED. Setting it makes
                #     WirePlumber treat the node as 'ready to be routed' and
                #     it adds an auto-link to the default sink in addition to
                #     the requested target.
                #   - node.autoconnect = false stops WirePlumber's session
                #     policy from auto-creating any link at all. Our explicit
                #     pw-link in spawn() is the only link that gets created.
                #   - node.dont-reconnect = true is belt + suspenders for the
                #     case where the link breaks at runtime — don't fall back
                #     to the default sink.
                #
                # Why this matters: with EasyEffects as default sink, an
                # auto-link from effect_output.SteelGameEQ to easyeffects_sink
                # creates a feedback loop (chain output → easyeffects → its
                # configured target SteelGame → loopback → chain input →
                # chain output ...). PipeWire detects the cycle and breaks
                # it by silencing one edge, so SteelGame audio never reaches
                # the headset.
                node.autoconnect    = false
                node.dont-reconnect = true
            }}
        }}
    }}
]
"#,
            desc = self.description,
            name = self.sink_name,
        )
    }
}

fn band_label(t: BandType) -> &'static str {
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

fn render_band_node(idx: usize, band: &EqBand) -> String {
    // Disabled bands stay in the graph but force gain to 0 dB so they're
    // audibly inert. Keeps the node/link topology constant across preset
    // swaps. Q is also clamped to a safe minimum — biquads with Q=0
    // produce NaN coefficients in some PipeWire builds.
    let gain = if band.enabled { band.gain } else { 0.0 };
    let q = band.q.max(0.0001);
    format!(
        r#"                    {{
                        type  = builtin
                        name  = eq_band_{idx}
                        label = {label}
                        control = {{ "Freq" = {freq:.4}  "Q" = {q:.4}  "Gain" = {gain:.4} }}
                    }}"#,
        idx = idx,
        label = band_label(band.band_type),
        freq = band.freq,
        q = q,
        gain = gain,
    )
}

/// Live filter-chain instance: owns the spawned `pipewire` child + its
/// on-disk config file. Dropping (or calling `shutdown`) kills the child
/// and removes the config file.
pub struct FilterChainHandle {
    child: Child,
    conf_path: PathBuf,
    sink_name: String,
}

impl FilterChainHandle {
    /// Where temp configs live. `$XDG_RUNTIME_DIR/steelvoicemix/filter-chains/`
    /// is ideal: tmpfs-backed, auto-cleaned on logout, scoped per-user.
    fn conf_dir() -> Option<PathBuf> {
        let base = std::env::var_os("XDG_RUNTIME_DIR").map(PathBuf::from)?;
        let dir = base.join("steelvoicemix").join("filter-chains");
        fs::create_dir_all(&dir).ok()?;
        Some(dir)
    }

    /// Write the spec to a config file and spawn `pipewire -c <conf>` to
    /// host the chain. Returns a handle to manage its lifecycle.
    ///
    /// After the spawn we wait briefly for the child to register its nodes,
    /// then explicitly link the chain's output ports to the playback target
    /// via `pw-link`. The `playback.props.node.target = <target>` directive
    /// in the conf is supposed to make wireplumber auto-create this link,
    /// but it's not reliable across all session-manager configurations
    /// (observed on KDE Plasma + WirePlumber on Fedora 43: chain spawns,
    /// ports register, but no link to the target gets created → audio
    /// reaches the chain's input but never makes it through). Doing it
    /// ourselves with `pw-link` is unambiguous.
    pub fn spawn(spec: &FilterChainSpec) -> Option<Self> {
        let conf_dir = Self::conf_dir()?;
        let conf_path = conf_dir.join(format!("{}.conf", spec.sink_name));

        let conf = spec.to_pipewire_conf();
        if let Err(e) = write_conf(&conf_path, &conf) {
            warn!("Failed to write filter-chain conf {}: {e}", conf_path.display());
            return None;
        }

        let child = Command::new("pipewire")
            .arg("-c")
            .arg(&conf_path)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|e| {
                warn!("Failed to spawn pipewire for filter-chain {}: {e}", spec.sink_name)
            })
            .ok()?;

        info!(
            "Spawned filter chain '{}' as pid {} (conf: {})",
            spec.sink_name,
            child.id(),
            conf_path.display()
        );

        // Wait for the spawned daemon to register its nodes, then
        // explicitly link the chain output to the playback target.
        // Earlier rev used a single 500 ms sleep + single pw-link
        // call; on cold boot (daemon restart while wireplumber is
        // still settling) the link silently failed and the user
        // had no audio until they toggled the feature off→on.
        // Retry with 500 ms × 8 attempts (up to 4 s total) so we
        // cover that startup window.
        let playback_node = spec.playback_node();
        for ch in ["FL", "FR"] {
            let from = format!("{playback_node}:output_{ch}");
            let to = format!("{}:playback_{ch}", spec.playback_target);
            let mut linked = false;
            let mut last_err = String::new();
            for attempt in 0..8 {
                std::thread::sleep(std::time::Duration::from_millis(500));
                let status = Command::new("pw-link")
                    .args([&from, &to])
                    .stderr(Stdio::piped())
                    .stdout(Stdio::null())
                    .output();
                match status {
                    Ok(out) if out.status.success() => {
                        info!(
                            "Linked {from} → {to} (attempt {})",
                            attempt + 1
                        );
                        linked = true;
                        break;
                    }
                    Ok(out) => {
                        last_err = String::from_utf8_lossy(&out.stderr)
                            .trim()
                            .to_string();
                    }
                    Err(e) => last_err = e.to_string(),
                }
            }
            if !linked {
                warn!(
                    "pw-link {from} → {to} failed after retries: {last_err}"
                );
            }
        }

        Some(FilterChainHandle {
            child,
            conf_path,
            sink_name: spec.sink_name.to_string(),
        })
    }

    /// Stop the child cleanly and remove its config file. Safe to call
    /// multiple times; the second call is a no-op because we've taken
    /// ownership of `self`.
    pub fn shutdown(mut self) {
        let pid = self.child.id();
        let _ = self.child.kill();
        let _ = self.child.wait();
        let _ = fs::remove_file(&self.conf_path);
        info!(
            "Filter chain '{}' (pid {pid}) shut down; removed {}",
            self.sink_name,
            self.conf_path.display()
        );
    }
}

impl Drop for FilterChainHandle {
    fn drop(&mut self) {
        // Best-effort cleanup if shutdown() wasn't called explicitly.
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
