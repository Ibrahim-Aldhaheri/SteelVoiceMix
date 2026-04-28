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
    /// `node.name` of the resulting filter-chain sink. Loopback targets
    /// reference this name (e.g. SteelGame.monitor → SteelGameEQ).
    pub sink_name: &'a str,
    /// Human-readable description shown in plasma-pa / pavucontrol.
    pub description: &'a str,
    /// PipeWire node name of the downstream target — typically the real
    /// headset sink name. The filter chain's playback side binds to this.
    pub playback_target: &'a str,
}

impl<'a> FilterChainSpec<'a> {
    /// Render the full pipewire daemon config (`context.modules = [ ... ]`)
    /// that, when run via `pipewire -c <file>`, loads exactly this filter
    /// chain into the running PipeWire instance.
    fn to_pipewire_conf(&self) -> String {
        // Phase 1 graph: stereo passthrough — two `copy` nodes, one per
        // channel. Real biquad bands and HeSuVi convolvers slot into the
        // same nodes/links/inputs/outputs structure later.
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
                    {{ type = builtin name = passL label = copy }}
                    {{ type = builtin name = passR label = copy }}
                ]
                inputs  = [ "passL:In"  "passR:In"  ]
                outputs = [ "passL:Out" "passR:Out" ]
            }}
            audio.channels = 2
            audio.position = [ FL FR ]
            capture.props = {{
                node.name   = "{name}"
                media.class = Audio/Sink
            }}
            playback.props = {{
                node.name   = "output.{name}"
                node.target = "{target}"
                node.passive = true
            }}
        }}
    }}
]
"#,
            desc = self.description,
            name = self.sink_name,
            target = self.playback_target,
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
