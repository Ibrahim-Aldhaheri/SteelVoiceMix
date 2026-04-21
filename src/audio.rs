//! Virtual sinks via `pactl load-module module-null-sink` plus a
//! `module-loopback` that forwards the null-sink's monitor to the real
//! headset. This is the Dymstro-era pattern and it's what KDE Plasma's
//! audio applet recognises — `pw-loopback` produced `input.`-prefixed
//! sinks that plasma-pa filters out.

use std::process::{Command, Stdio};

use log::{error, info, warn};

pub const GAME_SINK: &str = "NovaGame";
pub const CHAT_SINK: &str = "NovaChat";
pub const OUTPUT_MATCH: &str = "SteelSeries_Arctis_Nova_Pro_Wireless";

struct SinkModules {
    null_sink_id: u32,
    loopback_id: u32,
}

/// Manages the two virtual sinks and their loopbacks.
pub struct SinkManager {
    game: Option<SinkModules>,
    chat: Option<SinkModules>,
}

impl SinkManager {
    pub fn new() -> Self {
        // Sweep up anything a previous crash or manual test left behind.
        cleanup_stale_modules();
        SinkManager {
            game: None,
            chat: None,
        }
    }

    /// Auto-detect the Nova Pro Wireless PipeWire output sink name.
    pub fn find_output_sink() -> Option<String> {
        let output = Command::new("pactl")
            .args(["list", "sinks", "short"])
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .output()
            .ok()?;

        let stdout = String::from_utf8_lossy(&output.stdout);
        for line in stdout.lines() {
            if line.contains(OUTPUT_MATCH) && !line.contains("input.") {
                if let Some(name) = line.split('\t').nth(1) {
                    return Some(name.to_string());
                }
            }
        }
        None
    }

    /// Create the two virtual sinks routing to the given output sink.
    pub fn create_sinks(&mut self, output_sink: &str) {
        self.destroy_sinks();
        // Descriptions cannot contain spaces — pactl's proplist parser
        // splits sink_properties tokens on whitespace with no quote or
        // escape handling, so "Nova Game" would truncate to "Nova".
        self.game = create_sink_pair(output_sink, GAME_SINK, "Nova-Game");
        self.chat = create_sink_pair(output_sink, CHAT_SINK, "Nova-Chat");

        if self.game.is_some() && self.chat.is_some() {
            info!("Created sinks: {GAME_SINK}, {CHAT_SINK}");
        } else {
            error!("Failed to create one or more sinks");
        }
    }

    /// Unload the null-sink + loopback modules we created.
    pub fn destroy_sinks(&mut self) {
        if let Some(m) = self.game.take() {
            unload_module(m.loopback_id);
            unload_module(m.null_sink_id);
        }
        if let Some(m) = self.chat.take() {
            unload_module(m.loopback_id);
            unload_module(m.null_sink_id);
        }
    }

    /// Set volume on a sink (0–100) by its sink name.
    pub fn set_volume(sink: &str, volume: u8) {
        let vol_str = format!("{volume}%");
        let result = Command::new("pactl")
            .args(["set-sink-volume", sink, &vol_str])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn();

        if let Err(e) = result {
            warn!("Failed to set volume on {sink}: {e}");
        }
    }
}

impl Drop for SinkManager {
    fn drop(&mut self) {
        self.destroy_sinks();
    }
}

fn create_sink_pair(target: &str, name: &str, description: &str) -> Option<SinkModules> {
    // sink_properties is parsed token-by-token on spaces with no escape
    // or quote support. Values must be a single whitespace-free token.
    // node.description is the primary display string in plasma-pa;
    // node.nick is a secondary hint some UIs prefer.
    let null_sink_id = load_module(&[
        "module-null-sink",
        &format!("sink_name={name}"),
        &format!(
            "sink_properties=node.description={description} node.nick={description} device.description={description}"
        ),
    ])?;

    match load_module(&[
        "module-loopback",
        &format!("source={name}.monitor"),
        &format!("sink={target}"),
        "latency_msec=1",
    ]) {
        Some(loopback_id) => Some(SinkModules {
            null_sink_id,
            loopback_id,
        }),
        None => {
            warn!("Loopback for {name} failed — unloading its null-sink");
            unload_module(null_sink_id);
            None
        }
    }
}

fn load_module(args: &[&str]) -> Option<u32> {
    let output = Command::new("pactl")
        .arg("load-module")
        .args(args)
        .stderr(Stdio::null())
        .output()
        .ok()?;
    if !output.status.success() {
        warn!("pactl load-module failed: {:?}", args);
        return None;
    }
    String::from_utf8_lossy(&output.stdout)
        .trim()
        .parse::<u32>()
        .ok()
}

fn unload_module(id: u32) {
    let _ = Command::new("pactl")
        .args(["unload-module", &id.to_string()])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
}

/// Unload any NovaGame / NovaChat modules leaked by a previous run (crash,
/// kill -9, manual test). We identify them by their loaded arguments.
fn cleanup_stale_modules() {
    let Ok(output) = Command::new("pactl").args(["list", "modules"]).output() else {
        return;
    };
    let stdout = String::from_utf8_lossy(&output.stdout);
    let mut current_id: Option<u32> = None;
    for line in stdout.lines() {
        if let Some(rest) = line.strip_prefix("Module #") {
            current_id = rest.trim().parse::<u32>().ok();
            continue;
        }
        let trimmed = line.trim();
        if !trimmed.starts_with("Argument:") {
            continue;
        }
        if trimmed.contains("sink_name=NovaGame")
            || trimmed.contains("sink_name=NovaChat")
            || trimmed.contains("source=NovaGame.monitor")
            || trimmed.contains("source=NovaChat.monitor")
        {
            if let Some(id) = current_id {
                info!("Unloading stale Nova module #{id}");
                unload_module(id);
            }
        }
    }
}
