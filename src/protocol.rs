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
    #[serde(rename = "add-media-sink")]
    AddMediaSink,
    #[serde(rename = "remove-media-sink")]
    RemoveMediaSink,
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
        media_sink_enabled: bool,
    },

    /// Fired whenever the daemon adds or removes the SteelMedia sink —
    /// either from a CLI flag / runtime toggle, or from a GUI command.
    #[serde(rename = "media-sink-changed")]
    MediaSinkChanged { enabled: bool },
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::hid::BatteryStatus;
    use serde_json::{from_str, to_string, Value};

    #[test]
    fn client_command_status_parses() {
        let cmd: ClientCommand = from_str(r#"{"cmd":"status"}"#).unwrap();
        assert!(matches!(cmd, ClientCommand::Status));
    }

    #[test]
    fn client_command_subscribe_parses() {
        let cmd: ClientCommand = from_str(r#"{"cmd":"subscribe"}"#).unwrap();
        assert!(matches!(cmd, ClientCommand::Subscribe));
    }

    #[test]
    fn client_command_rejects_unknown_cmd() {
        let err: Result<ClientCommand, _> = from_str(r#"{"cmd":"nope"}"#);
        assert!(err.is_err());
    }

    #[test]
    fn chatmix_event_shape_matches_gui_contract() {
        // The Python GUI expects {"event":"chatmix","game":..,"chat":..}.
        // If this shape ever changes we break the GUI without a compile
        // error, so pin it.
        let ev = DaemonEvent::ChatMix { game: 80, chat: 20 };
        let json: Value = from_str(&to_string(&ev).unwrap()).unwrap();
        assert_eq!(json["event"], "chatmix");
        assert_eq!(json["game"], 80);
        assert_eq!(json["chat"], 20);
    }

    #[test]
    fn battery_event_shape_matches_gui_contract() {
        let ev = DaemonEvent::Battery {
            level: 75,
            status: "charging".into(),
        };
        let json: Value = from_str(&to_string(&ev).unwrap()).unwrap();
        assert_eq!(json["event"], "battery");
        assert_eq!(json["level"], 75);
        assert_eq!(json["status"], "charging");
    }

    #[test]
    fn connected_disconnected_events_have_only_event_tag() {
        for ev in [DaemonEvent::Connected, DaemonEvent::Disconnected] {
            let json: Value = from_str(&to_string(&ev).unwrap()).unwrap();
            assert!(json["event"].is_string());
            // Exactly one field in the object: event.
            assert_eq!(json.as_object().unwrap().len(), 1);
        }
    }

    #[test]
    fn status_event_carries_full_state_including_optional_battery() {
        let with_bat = DaemonEvent::Status {
            connected: true,
            game_vol: 60,
            chat_vol: 40,
            battery: Some(BatteryStatus {
                level: 80,
                status: "active".into(),
            }),
            media_sink_enabled: true,
        };
        let json: Value = from_str(&to_string(&with_bat).unwrap()).unwrap();
        assert_eq!(json["event"], "status");
        assert_eq!(json["connected"], true);
        assert_eq!(json["game_vol"], 60);
        assert_eq!(json["chat_vol"], 40);
        assert_eq!(json["battery"]["level"], 80);
        assert_eq!(json["battery"]["status"], "active");
        assert_eq!(json["media_sink_enabled"], true);

        let without_bat = DaemonEvent::Status {
            connected: false,
            game_vol: 100,
            chat_vol: 100,
            battery: None,
            media_sink_enabled: false,
        };
        let json: Value = from_str(&to_string(&without_bat).unwrap()).unwrap();
        assert!(json["battery"].is_null());
        assert_eq!(json["media_sink_enabled"], false);
    }

    #[test]
    fn client_command_media_sink_variants_parse() {
        assert!(matches!(
            from_str::<ClientCommand>(r#"{"cmd":"add-media-sink"}"#).unwrap(),
            ClientCommand::AddMediaSink
        ));
        assert!(matches!(
            from_str::<ClientCommand>(r#"{"cmd":"remove-media-sink"}"#).unwrap(),
            ClientCommand::RemoveMediaSink
        ));
    }

    #[test]
    fn media_sink_changed_event_shape() {
        let ev = DaemonEvent::MediaSinkChanged { enabled: true };
        let json: Value = from_str(&to_string(&ev).unwrap()).unwrap();
        assert_eq!(json["event"], "media-sink-changed");
        assert_eq!(json["enabled"], true);
    }
}
