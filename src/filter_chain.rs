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
        // (`/usr/share/pipewire/filter-chain/`). Critical idioms preserved
        // verbatim:
        //
        //   - Single node per band (not per channel). PipeWire auto-
        //     duplicates each node across channels driven by audio.channels.
        //     Earlier per-channel pairs (bassL/bassR) confused the graph.
        //   - Quoted control keys with float literals: "Freq" = 100.0 etc.
        //     Unquoted/integer forms parse but appear to produce silently
        //     broken biquad coefficients.
        //   - No explicit inputs/outputs arrays — derived from links.
        //   - playback.props uses node.passive=true (canonical for filter
        //     sinks) plus our explicit node.target so wireplumber routes
        //     output to the headset. Phase-1 attempt without node.passive
        //     made one direction work; explicit pw-link in spawn() is the
        //     safety net regardless.
        //
        // All gains default to 0.0 (passthrough — no audible change yet).
        // Phase 2.1 will expose these as user-controllable sliders that
        // respawn the chain with new values. The 6-band shape (low shelf
        // / 4 peaking / high shelf at 100, 100, 500, 2k, 5k, 5k Hz) is
        // PipeWire's stock starting point; we can adjust frequency
        // distribution later if the Sonar-style bands warrant it.
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
                    {{
                        type  = builtin
                        name  = eq_band_1
                        label = bq_lowshelf
                        control = {{ "Freq" = 100.0  "Q" = 1.0  "Gain" = 0.0 }}
                    }}
                    {{
                        type  = builtin
                        name  = eq_band_2
                        label = bq_peaking
                        control = {{ "Freq" = 100.0  "Q" = 1.0  "Gain" = 0.0 }}
                    }}
                    {{
                        type  = builtin
                        name  = eq_band_3
                        label = bq_peaking
                        control = {{ "Freq" = 500.0  "Q" = 1.0  "Gain" = 0.0 }}
                    }}
                    {{
                        type  = builtin
                        name  = eq_band_4
                        label = bq_peaking
                        control = {{ "Freq" = 2000.0  "Q" = 1.0  "Gain" = 0.0 }}
                    }}
                    {{
                        type  = builtin
                        name  = eq_band_5
                        label = bq_peaking
                        control = {{ "Freq" = 5000.0  "Q" = 1.0  "Gain" = 0.0 }}
                    }}
                    {{
                        type  = builtin
                        name  = eq_band_6
                        label = bq_highshelf
                        control = {{ "Freq" = 5000.0  "Q" = 1.0  "Gain" = 0.0 }}
                    }}
                ]
                links = [
                    {{ output = "eq_band_1:Out"  input = "eq_band_2:In" }}
                    {{ output = "eq_band_2:Out"  input = "eq_band_3:In" }}
                    {{ output = "eq_band_3:Out"  input = "eq_band_4:In" }}
                    {{ output = "eq_band_4:Out"  input = "eq_band_5:In" }}
                    {{ output = "eq_band_5:Out"  input = "eq_band_6:In" }}
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
            // playback_target is intentionally not interpolated into the
            // conf any more — see the comment block on playback.props
            // about why we no longer set node.target. The target is used
            // exclusively by the explicit pw-link in spawn() instead.
        )
    }
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

        // Wait for the spawned daemon to register its nodes before we
        // try to link them. 500 ms is empirically enough on a healthy box.
        std::thread::sleep(std::time::Duration::from_millis(500));

        // Wire chain output → target playback explicitly. Best effort: if
        // either side hasn't registered yet, this fails silently and the
        // user just gets no audio through the chain — same symptom as
        // before, no worse. In practice the 500 ms wait above is enough.
        let playback_node = spec.playback_node();
        for ch in ["FL", "FR"] {
            let from = format!("{playback_node}:output_{ch}");
            let to = format!("{}:playback_{ch}", spec.playback_target);
            let status = Command::new("pw-link")
                .args([&from, &to])
                .stderr(Stdio::piped())
                .stdout(Stdio::null())
                .output();
            match status {
                Ok(out) if out.status.success() => {
                    info!("Linked {from} → {to}");
                }
                Ok(out) => {
                    warn!(
                        "pw-link {from} → {to} failed: {}",
                        String::from_utf8_lossy(&out.stderr).trim()
                    );
                }
                Err(e) => {
                    warn!("pw-link {from} → {to} spawn error: {e}");
                }
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
