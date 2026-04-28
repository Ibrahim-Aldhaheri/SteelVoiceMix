//! PipeWire `module-filter-chain` integration.
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
//! For now the chain is a single passthrough `copy` node per channel —
//! audibly inert, but it proves the wiring. Real biquad EQ bands and the
//! HeSuVi convolver will plug into the same scaffolding without further
//! architectural change.

use std::process::{Command, Stdio};

use log::{info, warn};

/// Describes one filter-chain instance the daemon manages. The actual
/// audio nodes are built by `to_module_args()` and loaded via pactl.
pub struct FilterChainSpec<'a> {
    /// `node.name` of the resulting filter-chain sink. Loopback targets
    /// reference this name (e.g. SteelGame.monitor → SteelGameEQ).
    pub sink_name: &'a str,
    /// Human-readable description shown in plasma-pa / pavucontrol.
    pub description: &'a str,
    /// PipeWire node name of the downstream target — the real headset
    /// sink. The filter chain's playback side will bind to this.
    pub playback_target: &'a str,
}

impl<'a> FilterChainSpec<'a> {
    /// Build the single `pactl load-module module-filter-chain` argument
    /// string. PipeWire accepts the entire spec (filter graph + capture +
    /// playback wiring) as one quoted blob — simpler than writing a .conf
    /// file under `filter-chain.conf.d/` which would also live on disk
    /// after a daemon crash.
    fn to_module_args(&self) -> String {
        // For Phase 1 the graph is a stereo passthrough — two `copy`
        // nodes, one per channel. Adding biquad EQ bands later means
        // appending nodes + links inside this graph, no other change.
        format!(
            r#"node.description="{desc}" \
               filter.graph={{ \
                   nodes=[ \
                       {{ type=builtin name=passL label=copy }} \
                       {{ type=builtin name=passR label=copy }} \
                   ] \
                   inputs=[ "passL:In" "passR:In" ] \
                   outputs=[ "passL:Out" "passR:Out" ] \
               }} \
               audio.channels=2 \
               audio.position=[ FL FR ] \
               capture.props={{ \
                   node.name="{name}" \
                   media.class=Audio/Sink \
               }} \
               playback.props={{ \
                   node.name="output.{name}" \
                   node.target="{target}" \
                   node.passive=true \
               }}"#,
            desc = self.description,
            name = self.sink_name,
            target = self.playback_target,
        )
    }

    /// Load the filter chain via `pactl load-module module-filter-chain`.
    /// Returns the module ID for later unload, or None on failure.
    pub fn load(&self) -> Option<u32> {
        let args = self.to_module_args();
        let output = Command::new("pactl")
            .arg("load-module")
            .arg("module-filter-chain")
            .arg(&args)
            .stderr(Stdio::piped())
            .output()
            .ok()?;
        if !output.status.success() {
            warn!(
                "module-filter-chain load failed for {}: {}",
                self.sink_name,
                String::from_utf8_lossy(&output.stderr).trim()
            );
            return None;
        }
        let id = String::from_utf8_lossy(&output.stdout)
            .trim()
            .parse::<u32>()
            .ok()?;
        info!("Loaded filter chain '{}' as module #{id}", self.sink_name);
        Some(id)
    }
}

/// Unload a previously-loaded filter-chain module by its pactl module ID.
/// Best-effort — silently no-ops on failure since we usually call this
/// during sink teardown where retrying isn't useful.
pub fn unload(module_id: u32) {
    let _ = Command::new("pactl")
        .args(["unload-module", &module_id.to_string()])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
}
