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
    /// Build the SPA-JSON args object for `pw-cli load-module`. PipeWire's
    /// native loader accepts the same nested structure used in
    /// `filter-chain.conf.d/*.conf` files — graph + capture + playback
    /// wiring all in one. We deliberately do NOT use `pactl load-module`:
    /// the pulse-protocol bridge only proxies pulse-style key=value modules
    /// (null-sink, loopback) and rejects PipeWire-native filter-chain configs
    /// with an opaque "No such entity" error.
    fn to_module_args(&self) -> String {
        // Phase 1 graph is a stereo passthrough — two `copy` nodes, one
        // per channel. Real biquad EQ bands and HeSuVi convolvers slot
        // into the same nodes/links/inputs/outputs structure later.
        format!(
            "{{ \
                node.description=\"{desc}\" \
                filter.graph={{ \
                    nodes=[ \
                        {{ type=builtin name=passL label=copy }} \
                        {{ type=builtin name=passR label=copy }} \
                    ] \
                    inputs=[ \"passL:In\" \"passR:In\" ] \
                    outputs=[ \"passL:Out\" \"passR:Out\" ] \
                }} \
                audio.channels=2 \
                audio.position=[ FL FR ] \
                capture.props={{ \
                    node.name=\"{name}\" \
                    media.class=Audio/Sink \
                }} \
                playback.props={{ \
                    node.name=\"output.{name}\" \
                    node.target=\"{target}\" \
                    node.passive=true \
                }} \
            }}",
            desc = self.description,
            name = self.sink_name,
            target = self.playback_target,
        )
    }

    /// Load the filter chain via `pw-cli load-module`. Returns the
    /// PipeWire object ID (to pass to `unload`), or None on failure.
    /// pw-cli prints a line like `Object: 12345` on success.
    pub fn load(&self) -> Option<u32> {
        let args = self.to_module_args();
        let output = Command::new("pw-cli")
            .arg("load-module")
            .arg("libpipewire-module-filter-chain")
            .arg(&args)
            .stderr(Stdio::piped())
            .stdout(Stdio::piped())
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
        // pw-cli's "load-module" output: typically a single integer (the
        // object ID) on stdout. Older builds print "Object: N"; newer
        // ones just "N". Parse the first integer-looking token we see.
        let stdout = String::from_utf8_lossy(&output.stdout);
        let id = stdout
            .split_whitespace()
            .find_map(|tok| tok.trim_matches(|c: char| !c.is_ascii_digit()).parse::<u32>().ok())?;
        info!(
            "Loaded filter chain '{}' as PipeWire object #{id}",
            self.sink_name
        );
        Some(id)
    }
}

/// Unload a previously-loaded filter-chain by its PipeWire object ID.
/// Best-effort — silently no-ops on failure since we usually call this
/// during sink teardown where retrying isn't useful.
pub fn unload(object_id: u32) {
    let _ = Command::new("pw-cli")
        .args(["destroy", &object_id.to_string()])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
}
