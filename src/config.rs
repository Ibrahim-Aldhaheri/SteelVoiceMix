//! Persistent daemon state (separate from the GUI's settings.json).
//!
//! Stored at `$XDG_CONFIG_HOME/steelvoicemix/daemon.json`. Owned
//! exclusively by the Rust daemon — the Python GUI signals its
//! preferences over the socket instead of touching this file.

use std::fs;
use std::io::Write;
use std::path::PathBuf;

use serde::{Deserialize, Serialize};

/// What the daemon remembers across restarts. Default-derived: a fresh
/// install loads as all-false so we don't surprise anyone with extra
/// output devices they didn't ask for.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct DaemonState {
    #[serde(default)]
    pub media_sink_enabled: bool,
    #[serde(default)]
    pub hdmi_sink_enabled: bool,
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
