//! PipeWire sink creation and volume control via pw-loopback and pactl.

use std::process::{Child, Command, Stdio};

use log::{error, info, warn};

pub const GAME_SINK: &str = "NovaGame";
pub const CHAT_SINK: &str = "NovaChat";
pub const OUTPUT_MATCH: &str = "SteelSeries_Arctis_Nova_Pro_Wireless";

/// Manages the two virtual PipeWire loopback sinks.
pub struct SinkManager {
    game_loopback: Option<Child>,
    chat_loopback: Option<Child>,
}

impl SinkManager {
    pub fn new() -> Self {
        SinkManager {
            game_loopback: None,
            chat_loopback: None,
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
            if line.contains(OUTPUT_MATCH) {
                // pactl short format: ID\tNAME\tDRIVER\tFORMAT\tSTATE
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

        self.game_loopback = spawn_loopback(output_sink, GAME_SINK);
        self.chat_loopback = spawn_loopback(output_sink, CHAT_SINK);

        if self.game_loopback.is_some() && self.chat_loopback.is_some() {
            info!("Created sinks: {GAME_SINK}, {CHAT_SINK}");
        } else {
            error!("Failed to create one or more sinks");
        }
    }

    /// Terminate virtual sinks.
    pub fn destroy_sinks(&mut self) {
        kill_child(&mut self.game_loopback);
        kill_child(&mut self.chat_loopback);
    }

    /// Set volume on a sink (0–100). Uses the sink name directly (bug fix: Python used `input.{sink}`).
    pub fn set_volume(sink: &str, volume: u8) {
        let vol_str = format!("{}%", volume);
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

fn spawn_loopback(output_sink: &str, name: &str) -> Option<Child> {
    Command::new("pw-loopback")
        .args([
            "-P",
            output_sink,
            "--capture-props=media.class=Audio/Sink",
            "-n",
            name,
        ])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|e| error!("Failed to spawn pw-loopback for {name}: {e}"))
        .ok()
}

fn kill_child(child: &mut Option<Child>) {
    if let Some(ref mut c) = child {
        let _ = c.kill();
        let _ = c.wait();
    }
    *child = None;
}
