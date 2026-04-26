//! Auto-routing of browser / media-player streams to the SteelMedia sink.
//!
//! Polls `pactl -f json list sink-inputs` on a fixed interval. For any new
//! sink-input whose `application.process.binary` matches a known browser or
//! video-player binary, move it to SteelMedia (if SteelMedia exists).
//!
//! Why polling instead of subscribing to PulseAudio events: subscribing
//! requires the libpulse async client bindings, which we don't link.
//! Polling at 5s costs ~one cheap `pactl` exec per interval and is fine for
//! a feature that doesn't need sub-second response.
//!
//! Stream IDs we've already routed are remembered, so users who manually
//! re-route a stream back (e.g. via Plasma's audio applet) don't get
//! their choice fought every cycle. We only act on first-seen streams.

use std::collections::HashSet;
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use log::{debug, info, warn};
use serde::Deserialize;

use crate::audio::MEDIA_SINK;

/// Process binaries we route to Media by default. User-extensible later
/// (config file or socket command); for now hard-coded based on what the
/// upstream Arctis Sound Manager handles.
const DEFAULT_MATCH_BINARIES: &[&str] = &[
    "firefox",
    "firefox-bin",
    "chromium",
    "chromium-browser",
    "brave",
    "brave-browser",
    "librewolf",
    "vivaldi",
    "google-chrome",
    "chrome",
    "mpv",
    "vlc",
    "haruna",
    "celluloid",
    "smplayer",
];

const POLL_INTERVAL: Duration = Duration::from_secs(5);

/// Subset of pactl's JSON sink-input output we actually need.
#[derive(Debug, Deserialize)]
struct SinkInput {
    index: u32,
    properties: SinkInputProps,
}

#[derive(Debug, Deserialize)]
struct SinkInputProps {
    #[serde(rename = "application.process.binary", default)]
    process_binary: Option<String>,
}

/// Toggle + tracking state, owned by the BrowserRouter thread.
pub struct RouterState {
    pub enabled: AtomicBool,
    seen: Mutex<HashSet<u32>>,
}

impl RouterState {
    pub fn new(enabled: bool) -> Self {
        RouterState {
            enabled: AtomicBool::new(enabled),
            seen: Mutex::new(HashSet::new()),
        }
    }
}

/// Spawn the background router thread. Returns immediately. Thread exits
/// when `running` flips false.
pub fn spawn_router(state: Arc<RouterState>, running: Arc<AtomicBool>) {
    thread::spawn(move || {
        info!("Browser auto-routing thread started");
        while running.load(Ordering::Relaxed) {
            thread::sleep(POLL_INTERVAL);
            if !state.enabled.load(Ordering::Relaxed) {
                continue;
            }
            if let Err(e) = scan_and_route(&state) {
                debug!("auto-route scan error: {e}");
            }
        }
        info!("Browser auto-routing thread exiting");
    });
}

fn scan_and_route(state: &RouterState) -> Result<(), String> {
    // Skip if SteelMedia isn't loaded — there's nowhere to route to.
    if !sink_exists(MEDIA_SINK) {
        return Ok(());
    }

    let inputs = list_sink_inputs()?;
    let mut seen = state.seen.lock().unwrap();
    for input in inputs {
        if seen.contains(&input.index) {
            continue;
        }
        seen.insert(input.index);

        let Some(binary) = input.properties.process_binary.as_deref() else {
            continue;
        };
        if !DEFAULT_MATCH_BINARIES.iter().any(|b| binary.eq_ignore_ascii_case(b)) {
            continue;
        }
        info!(
            "Auto-routing sink-input #{} ({}) to {}",
            input.index, binary, MEDIA_SINK
        );
        if let Err(e) = move_sink_input(input.index, MEDIA_SINK) {
            warn!("Failed to move stream #{}: {e}", input.index);
        }
    }
    Ok(())
}

fn list_sink_inputs() -> Result<Vec<SinkInput>, String> {
    let output = Command::new("pactl")
        .args(["-f", "json", "list", "sink-inputs"])
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .output()
        .map_err(|e| format!("spawn pactl: {e}"))?;
    if !output.status.success() {
        return Err("pactl list sink-inputs returned non-zero".into());
    }
    serde_json::from_slice::<Vec<SinkInput>>(&output.stdout)
        .map_err(|e| format!("parse json: {e}"))
}

fn sink_exists(sink_name: &str) -> bool {
    let Ok(output) = Command::new("pactl")
        .args(["list", "short", "sinks"])
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .output()
    else {
        return false;
    };
    String::from_utf8_lossy(&output.stdout)
        .lines()
        .any(|l| l.split('\t').nth(1) == Some(sink_name))
}

fn move_sink_input(input_id: u32, sink_name: &str) -> Result<(), String> {
    let status = Command::new("pactl")
        .args(["move-sink-input", &input_id.to_string(), sink_name])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map_err(|e| format!("spawn pactl move: {e}"))?;
    if !status.success() {
        return Err("pactl move-sink-input returned non-zero".into());
    }
    Ok(())
}
