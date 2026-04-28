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
    #[serde(rename = "add-hdmi-sink")]
    AddHdmiSink,
    #[serde(rename = "remove-hdmi-sink")]
    RemoveHdmiSink,
    /// Toggle browser auto-routing: any subsequent new sink-input from a
    /// known browser/media-player binary will be moved to SteelMedia.
    #[serde(rename = "set-auto-route-browsers")]
    SetAutoRouteBrowsers { enabled: bool },
    /// Toggle EQ filter-chain insertion on Game + Chat. Phase 1 chain is
    /// passthrough so this is audibly inert; the toggle proves the
    /// architecture before bands land.
    #[serde(rename = "set-eq-enabled")]
    SetEqEnabled { enabled: bool },
    /// Set the gain (dB) of one EQ band. Bands are 1..=6 in the order:
    /// low shelf @ 100, peaking @ 100, peaking @ 500, peaking @ 2000,
    /// peaking @ 5000, high shelf @ 5000 Hz. Daemon clamps gain to
    /// [-12.0, 12.0]. If EQ is currently enabled, the chain respawns
    /// with the new gain (~100 ms audio glitch).
    #[serde(rename = "set-eq-band-gain")]
    SetEqBandGain { band: u8, gain_db: f32 },
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
        hdmi_sink_enabled: bool,
        auto_route_browsers: bool,
        eq_enabled: bool,
        eq_band_gains: [f32; 6],
    },

    /// Fired whenever the daemon adds or removes the SteelMedia sink —
    /// either from a CLI flag / runtime toggle, or from a GUI command.
    #[serde(rename = "media-sink-changed")]
    MediaSinkChanged { enabled: bool },

    /// Fired whenever the daemon adds or removes the SteelHDMI sink.
    #[serde(rename = "hdmi-sink-changed")]
    HdmiSinkChanged { enabled: bool },

    /// Fired when browser auto-routing is toggled.
    #[serde(rename = "auto-route-browsers-changed")]
    AutoRouteBrowsersChanged { enabled: bool },

    /// Fired when EQ insertion is toggled.
    #[serde(rename = "eq-enabled-changed")]
    EqEnabledChanged { enabled: bool },

    /// Fired whenever any EQ band's gain changes — emits the full
    /// 6-band array so the GUI can refresh all sliders at once and
    /// stay in sync if multiple changes happen close together.
    #[serde(rename = "eq-band-gains-changed")]
    EqBandGainsChanged { gains: [f32; 6] },
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
            hdmi_sink_enabled: false,
            auto_route_browsers: false,
            eq_enabled: false,
            eq_band_gains: [0.0; 6],
        };
        let json: Value = from_str(&to_string(&with_bat).unwrap()).unwrap();
        assert_eq!(json["event"], "status");
        assert_eq!(json["connected"], true);
        assert_eq!(json["game_vol"], 60);
        assert_eq!(json["chat_vol"], 40);
        assert_eq!(json["battery"]["level"], 80);
        assert_eq!(json["battery"]["status"], "active");
        assert_eq!(json["media_sink_enabled"], true);
        assert_eq!(json["hdmi_sink_enabled"], false);
        assert_eq!(json["auto_route_browsers"], false);
        assert_eq!(json["eq_enabled"], false);
        assert!(json["eq_band_gains"].is_array());

        let without_bat = DaemonEvent::Status {
            connected: false,
            game_vol: 100,
            chat_vol: 100,
            battery: None,
            media_sink_enabled: false,
            hdmi_sink_enabled: true,
            auto_route_browsers: true,
            eq_enabled: true,
            eq_band_gains: [-3.0, 0.0, 0.0, 0.0, 0.0, 6.0],
        };
        let json: Value = from_str(&to_string(&without_bat).unwrap()).unwrap();
        assert!(json["battery"].is_null());
        assert_eq!(json["media_sink_enabled"], false);
        assert_eq!(json["hdmi_sink_enabled"], true);
        assert_eq!(json["auto_route_browsers"], true);
        assert_eq!(json["eq_enabled"], true);
        assert_eq!(json["eq_band_gains"][0], -3.0);
        assert_eq!(json["eq_band_gains"][5], 6.0);
    }

    #[test]
    fn set_eq_band_gain_command_parses() {
        let cmd: ClientCommand =
            from_str(r#"{"cmd":"set-eq-band-gain","band":3,"gain_db":-4.5}"#).unwrap();
        match cmd {
            ClientCommand::SetEqBandGain { band, gain_db } => {
                assert_eq!(band, 3);
                assert!((gain_db - -4.5).abs() < 1e-6);
            }
            other => panic!("expected SetEqBandGain, got {other:?}"),
        }
    }

    #[test]
    fn eq_band_gains_changed_event_shape() {
        let ev = DaemonEvent::EqBandGainsChanged {
            gains: [1.0, -2.0, 0.0, 0.0, 0.0, 0.0],
        };
        let json: Value = from_str(&to_string(&ev).unwrap()).unwrap();
        assert_eq!(json["event"], "eq-band-gains-changed");
        assert_eq!(json["gains"][0], 1.0);
        assert_eq!(json["gains"][1], -2.0);
    }

    #[test]
    fn set_eq_enabled_command_parses() {
        let cmd: ClientCommand =
            from_str(r#"{"cmd":"set-eq-enabled","enabled":true}"#).unwrap();
        match cmd {
            ClientCommand::SetEqEnabled { enabled } => assert!(enabled),
            other => panic!("expected SetEqEnabled, got {other:?}"),
        }
    }

    #[test]
    fn eq_enabled_changed_event_shape() {
        let ev = DaemonEvent::EqEnabledChanged { enabled: true };
        let json: Value = from_str(&to_string(&ev).unwrap()).unwrap();
        assert_eq!(json["event"], "eq-enabled-changed");
        assert_eq!(json["enabled"], true);
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
    fn client_command_hdmi_sink_variants_parse() {
        assert!(matches!(
            from_str::<ClientCommand>(r#"{"cmd":"add-hdmi-sink"}"#).unwrap(),
            ClientCommand::AddHdmiSink
        ));
        assert!(matches!(
            from_str::<ClientCommand>(r#"{"cmd":"remove-hdmi-sink"}"#).unwrap(),
            ClientCommand::RemoveHdmiSink
        ));
    }

    #[test]
    fn media_sink_changed_event_shape() {
        let ev = DaemonEvent::MediaSinkChanged { enabled: true };
        let json: Value = from_str(&to_string(&ev).unwrap()).unwrap();
        assert_eq!(json["event"], "media-sink-changed");
        assert_eq!(json["enabled"], true);
    }

    #[test]
    fn hdmi_sink_changed_event_shape() {
        let ev = DaemonEvent::HdmiSinkChanged { enabled: true };
        let json: Value = from_str(&to_string(&ev).unwrap()).unwrap();
        assert_eq!(json["event"], "hdmi-sink-changed");
        assert_eq!(json["enabled"], true);
    }

    #[test]
    fn set_auto_route_browsers_command_parses() {
        let cmd: ClientCommand =
            from_str(r#"{"cmd":"set-auto-route-browsers","enabled":true}"#).unwrap();
        match cmd {
            ClientCommand::SetAutoRouteBrowsers { enabled } => assert!(enabled),
            other => panic!("expected SetAutoRouteBrowsers, got {other:?}"),
        }
    }

    #[test]
    fn auto_route_browsers_changed_event_shape() {
        let ev = DaemonEvent::AutoRouteBrowsersChanged { enabled: true };
        let json: Value = from_str(&to_string(&ev).unwrap()).unwrap();
        assert_eq!(json["event"], "auto-route-browsers-changed");
        assert_eq!(json["enabled"], true);
    }
}
