//! Persistent daemon state (separate from the GUI's settings.json).
//!
//! Stored at `$XDG_CONFIG_HOME/steelvoicemix/daemon.json`. Owned
//! exclusively by the Rust daemon — the Python GUI signals its
//! preferences over the socket instead of touching this file.

use std::fs;
use std::io::Write;
use std::path::PathBuf;

use serde::{Deserialize, Serialize};

use crate::protocol::{EqState, MicState};

/// What the daemon remembers across restarts. Most fields default to
/// "off" so a fresh install doesn't surprise users with extra output
/// devices. The exception is `surround_enabled`, which is on by
/// default — see its field comment.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DaemonState {
    #[serde(default)]
    pub media_sink_enabled: bool,
    #[serde(default)]
    pub hdmi_sink_enabled: bool,
    /// When true, the routing thread auto-moves browser/media-player
    /// sink-inputs to SteelMedia (so they bypass the ChatMix dial).
    /// Off by default — opt-in to avoid surprising users.
    #[serde(default)]
    pub auto_route_browsers: bool,
    /// When true, EQ filter chains are inserted on Game + Chat at
    /// startup. Toggling this re-runs the loopback-swap dance.
    #[serde(default)]
    pub eq_enabled: bool,
    /// Full per-band EQ state for both channels. Each channel carries
    /// 10 `EqBand`s (freq / Q / gain / type / enabled). Default = flat
    /// passthrough at standard graphic-EQ frequencies. Preset JSONs in
    /// the `parametricEQ.filter1..filter10` shape map directly here.
    #[serde(default, alias = "eq_gains")]
    pub eq_state: EqState,
    /// Whether the SteelSurround virtual 7.1 sink + HRIR convolver
    /// chain is loaded. ON by default — anyone running this app has an
    /// Arctis Nova Pro Wireless and will benefit from binaural surround
    /// out of the box. The GUI auto-applies the bundled HRIR file
    /// (gui/data/hrir/EAC_Default.wav) on first launch so this flag
    /// has something to bind to. Users who don't want it can disable
    /// it from the Surround tab.
    #[serde(default = "default_surround_enabled")]
    pub surround_enabled: bool,
    /// Path to the user-supplied HRIR WAV (HeSuVi-style 14-channel).
    /// `None` until the user picks a file via the GUI; surround can't
    /// be enabled while this is `None`.
    #[serde(default)]
    pub surround_hrir_path: Option<PathBuf>,
    /// Microphone capture-side processing state (noise gate, NR, AI
    /// NC). Each feature persists independently; daemon spawns one
    /// LADSPA filter chain covering whichever combination is on.
    #[serde(default)]
    pub mic_state: MicState,
    /// Headset hardware sidetone level (0..=128, normalised; daemon
    /// maps to the device's 4-step internal setting). Persisted so
    /// the daemon can restore it on reconnect — the headset's EEPROM
    /// already remembers across power cycles, but we re-send on each
    /// connect to handle the case where the user switched between
    /// machines.
    #[serde(default = "default_sidetone_level")]
    pub sidetone_level: u8,
    /// Whether the daemon emits desktop notifications via notify-send
    /// on connect/disconnect events. Defaults true (consistent with
    /// the legacy --no-notify CLI flag's "off-only" semantics).
    #[serde(default = "default_notifications_enabled")]
    pub notifications_enabled: bool,
}

fn default_surround_enabled() -> bool {
    true
}

fn default_sidetone_level() -> u8 {
    0
}

fn default_notifications_enabled() -> bool {
    true
}

impl Default for DaemonState {
    fn default() -> Self {
        DaemonState {
            media_sink_enabled: false,
            hdmi_sink_enabled: false,
            auto_route_browsers: false,
            eq_enabled: false,
            eq_state: EqState::default(),
            surround_enabled: default_surround_enabled(),
            surround_hrir_path: None,
            mic_state: MicState::default(),
            sidetone_level: default_sidetone_level(),
            notifications_enabled: default_notifications_enabled(),
        }
    }
}

fn state_path() -> Option<PathBuf> {
    let base = std::env::var_os("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .or_else(|| {
            std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".config"))
        })?;
    Some(base.join("steelvoicemix").join("daemon.json"))
}

/// Load saved state, falling back to defaults if the file is missing or
/// malformed. Never panics.
pub fn load() -> DaemonState {
    let Some(path) = state_path() else {
        return DaemonState::default();
    };
    let Ok(contents) = fs::read_to_string(&path) else {
        return DaemonState::default();
    };
    serde_json::from_str(&contents).unwrap_or_default()
}

/// Write state to disk atomically (tmp + rename). Best effort — we only
/// log on failure so a broken home dir doesn't take down the daemon.
pub fn save(state: &DaemonState) {
    let Some(path) = state_path() else {
        log::warn!("No config directory available; skipping daemon state save");
        return;
    };
    if let Some(parent) = path.parent() {
        if let Err(e) = fs::create_dir_all(parent) {
            log::warn!("Could not create {}: {e}", parent.display());
            return;
        }
    }

    let json = match serde_json::to_string_pretty(state) {
        Ok(s) => s,
        Err(e) => {
            log::warn!("Could not serialize daemon state: {e}");
            return;
        }
    };

    let tmp = path.with_extension("json.tmp");
    let write_result = fs::File::create(&tmp).and_then(|mut f| {
        f.write_all(json.as_bytes())?;
        f.write_all(b"\n")
    });
    if let Err(e) = write_result {
        log::warn!("Could not write {}: {e}", tmp.display());
        return;
    }
    if let Err(e) = fs::rename(&tmp, &path) {
        log::warn!("Could not rename {} -> {}: {e}", tmp.display(), path.display());
    }
}
