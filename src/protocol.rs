//! JSON protocol for communication between the Rust daemon and GUI clients
//! over a Unix domain socket.

use serde::{Deserialize, Serialize};

use crate::hid::BatteryStatus;

/// Commands sent by the GUI client to the daemon.
#[derive(Debug, Deserialize)]
#[serde(tag = "cmd")]
pub enum ClientCommand {
    #[serde(rename = "subscribe")]
    Subscribe,
    #[serde(rename = "status")]
    Status,
}

/// Events sent by the daemon to subscribed GUI clients.
#[derive(Debug, Clone, Serialize)]
#[serde(tag = "event")]
pub enum DaemonEvent {
    #[serde(rename = "chatmix")]
    ChatMix { game: u8, chat: u8 },

    #[serde(rename = "battery")]
    Battery {
        level: u8,
        status: String,
    },

    #[serde(rename = "connected")]
    Connected,

    #[serde(rename = "disconnected")]
    Disconnected,

    #[serde(rename = "status")]
    Status {
        connected: bool,
        game_vol: u8,
        chat_vol: u8,
        battery: Option<BatteryStatus>,
    },
}
