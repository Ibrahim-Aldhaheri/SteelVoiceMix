//! JSON protocol for communication between the Rust daemon and GUI clients
//! over a Unix domain socket.

use serde::{Deserialize, Serialize};

use crate::hid::BatteryStatus;

/// Which audio channel an EQ command targets. Per-channel EQ — every
/// virtual sink gets its own band set, so a user can tune game audio
/// bass-heavy, chat audio mid-forward, and media audio cinematic
/// independently. Media and Hdmi only do anything when the
/// corresponding null-sinks are loaded; if they aren't, an EQ command
/// for those channels just updates persistent state and waits for the
/// next time the sink comes up.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum EqChannel {
    Game,
    Chat,
    Media,
    Hdmi,
    /// Microphone capture path. Lives between the Gate/NR/AI-NC
    /// stages and the SteelMic virtual source — see mic_chain.rs.
    /// Distinct from the output channels because the bands run
    /// inside the same PipeWire process the mic-feature stages do
    /// (no separate filter chain, no extra null-sink).
    Mic,
}

/// Number of EQ bands per channel. Common parametric-EQ preset JSONs
/// carry exactly `parametricEQ.filter1..filter10` — 10 bands here lets
/// those load 1:1 without padding or truncation.
pub const NUM_BANDS: usize = 10;

/// Filter type for a single biquad band. Names match PipeWire's
/// `bq_*` builtin labels; the wire format is lowercase JSON
/// (`"lowshelf"` etc.) for parity with the `parametricEQ` preset shape.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum BandType {
    Lowshelf,
    Peaking,
    Highshelf,
    Lowpass,
    Highpass,
    Bandpass,
    Notch,
    Allpass,
}

/// One EQ band's parameters. Maps directly to a PipeWire builtin biquad
/// node and the `parametricEQ.filterN` shape used by common preset JSONs.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct EqBand {
    pub freq: f32,
    pub q: f32,
    pub gain: f32,
    #[serde(rename = "type")]
    pub band_type: BandType,
    pub enabled: bool,
}

impl EqBand {
    /// Convenience constructor for the common case.
    pub const fn new(freq: f32, q: f32, gain: f32, band_type: BandType) -> Self {
        EqBand {
            freq,
            q,
            gain,
            band_type,
            enabled: true,
        }
    }
}

impl Default for EqBand {
    fn default() -> Self {
        EqBand::new(1000.0, 1.0, 0.0, BandType::Peaking)
    }
}

/// Default 10-band shape — standard graphic-EQ frequencies, all gains
/// at 0 (passthrough). When a preset loads, every band's freq / q /
/// gain / type can be replaced; the slider UI tracks whatever the
/// current band parameters are.
pub fn default_channel_bands() -> [EqBand; NUM_BANDS] {
    [
        EqBand::new(   32.0, 0.7, 0.0, BandType::Lowshelf),
        EqBand::new(   64.0, 1.0, 0.0, BandType::Peaking),
        EqBand::new(  125.0, 1.0, 0.0, BandType::Peaking),
        EqBand::new(  250.0, 1.0, 0.0, BandType::Peaking),
        EqBand::new(  500.0, 1.0, 0.0, BandType::Peaking),
        EqBand::new( 1000.0, 1.0, 0.0, BandType::Peaking),
        EqBand::new( 2000.0, 1.0, 0.0, BandType::Peaking),
        EqBand::new( 4000.0, 1.0, 0.0, BandType::Peaking),
        EqBand::new( 8000.0, 1.0, 0.0, BandType::Peaking),
        EqBand::new(16000.0, 0.7, 0.0, BandType::Highshelf),
    ]
}

/// One microphone-side processing feature's runtime state. Strength
/// is 0..=100 — interpretation is per-feature (threshold for the
/// gate, VAD aggressiveness for the denoisers).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
pub struct MicFeature {
    pub enabled: bool,
    pub strength: u8,
}

/// Microphone capture-path processing — three independent features,
/// any combination can be on. Daemon spawns one PipeWire filter-chain
/// instance covering the currently-enabled features in fixed order:
/// gate → noise reduction → AI denoise → virtual SteelMic source.
/// `noise_reduction` and `ai_noise_cancellation` both run the
/// noise-suppression-for-voice RNNoise LADSPA plugin; the difference
/// is the VAD-threshold mapping (NR is mild, AI NC aggressive). If
/// both are enabled, AI NC's stage takes precedence to avoid running
/// RNNoise twice in sequence.
/// Which LADSPA plugin powers the Volume Stabilizer feature.
/// Both ship in ladspa-swh-plugins (Steve Harris, GPL), so neither
/// needs a separate dependency. The choice is purely tonal:
///   - Broadcast (SC4 mono): canonical voice compressor, audibly
///     levels loud / quiet swings. Default.
///   - Soft (Dyson): older, much gentler — barely audible at any
///     setting. Kept for users who want a transparent option.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "lowercase")]
pub enum VolumeStabilizerKind {
    #[default]
    Broadcast,
    Soft,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
pub struct MicState {
    #[serde(default)]
    pub noise_gate: MicFeature,
    #[serde(default)]
    pub noise_reduction: MicFeature,
    #[serde(default)]
    pub ai_noise_cancellation: MicFeature,
    /// Volume Stabilizer — smooths level swings between quiet
    /// whispers and loud bursts. Plugin is picked by
    /// `volume_stabilizer_kind`; strength 0..=100 drives whichever
    /// parameter best maps to "more compression" for that plugin.
    #[serde(default)]
    pub volume_stabilizer: MicFeature,
    #[serde(default)]
    pub volume_stabilizer_kind: VolumeStabilizerKind,
}

/// Per-channel band arrays bundled into one persistent struct. Media
/// and Hdmi default to flat just like Game/Chat — even if the user
/// never enables those sinks, the state survives so toggling EQ on
/// later doesn't lose tuning they made before.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct EqState {
    pub game: [EqBand; NUM_BANDS],
    pub chat: [EqBand; NUM_BANDS],
    #[serde(default = "default_channel_bands")]
    pub media: [EqBand; NUM_BANDS],
    #[serde(default = "default_channel_bands")]
    pub hdmi: [EqBand; NUM_BANDS],
    #[serde(default = "default_channel_bands")]
    pub mic: [EqBand; NUM_BANDS],
}

impl Default for EqState {
    fn default() -> Self {
        EqState {
            game: default_channel_bands(),
            chat: default_channel_bands(),
            media: default_channel_bands(),
            hdmi: default_channel_bands(),
            mic: default_channel_bands(),
        }
    }
}

impl EqState {
    pub fn for_channel(&self, channel: EqChannel) -> [EqBand; NUM_BANDS] {
        match channel {
            EqChannel::Game => self.game,
            EqChannel::Chat => self.chat,
            EqChannel::Media => self.media,
            EqChannel::Hdmi => self.hdmi,
            EqChannel::Mic => self.mic,
        }
    }

    pub fn for_channel_mut(&mut self, channel: EqChannel) -> &mut [EqBand; NUM_BANDS] {
        match channel {
            EqChannel::Game => &mut self.game,
            EqChannel::Chat => &mut self.chat,
            EqChannel::Media => &mut self.media,
            EqChannel::Hdmi => &mut self.hdmi,
            EqChannel::Mic => &mut self.mic,
        }
    }
}

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
    /// Set just the gain (dB) of one EQ band on one channel. Bands are
    /// 1..=10 (NUM_BANDS). Daemon clamps gain to [-12.0, 12.0]. The
    /// other band parameters (freq, Q, type, enabled) stay unchanged —
    /// useful for slider drags where only gain moves. If EQ is enabled,
    /// only the affected channel's chain respawns.
    #[serde(rename = "set-eq-band-gain")]
    SetEqBandGain {
        channel: EqChannel,
        band: u8,
        gain_db: f32,
    },
    /// Replace one EQ band wholesale — used when loading a preset
    /// where freq, Q, gain, and type all change at once. `band` is
    /// 1..=NUM_BANDS. Same chain-respawn behaviour as SetEqBandGain.
    #[serde(rename = "set-eq-band")]
    SetEqBand {
        channel: EqChannel,
        band: u8,
        params: EqBand,
    },
    /// Replace ALL bands on a channel atomically. Used when loading a
    /// preset — sending 10 individual SetEqBand calls would respawn the
    /// channel's filter chain 10 times in a row (and emit 10 broadcast
    /// events). This batches them into a single respawn + one event.
    #[serde(rename = "set-eq-channel")]
    SetEqChannel {
        channel: EqChannel,
        bands: [EqBand; NUM_BANDS],
    },
    /// Toggle the SteelSurround virtual 7.1 sink + HRIR convolver
    /// chain. Requires `set-surround-hrir` to have been called first
    /// with a valid HRIR WAV path; the daemon refuses with a warning
    /// otherwise.
    #[serde(rename = "set-surround-enabled")]
    SetSurroundEnabled { enabled: bool },
    /// Set the HRIR file path used by the surround convolver. Path is
    /// stored persistently; the user supplies their own HRIR (e.g. from
    /// HeSuVi or Impulcifer) — we don't bundle one for licensing
    /// reasons. Sending `null` (or an empty string) clears the path.
    #[serde(rename = "set-surround-hrir")]
    SetSurroundHrir { path: Option<String> },
    /// Reset every runtime preference to its default: media + HDMI
    /// sinks off, browser auto-routing off, EQ off + flat across all
    /// channels, surround off + HRIR cleared. Persisted state is
    /// rewritten with the defaults. Used by the GUI's "Reset to
    /// defaults" button. The GUI's own settings.json (profiles,
    /// overlay options, etc.) is reset GUI-side.
    #[serde(rename = "reset-state")]
    ResetState,
    /// Toggle / parameterise the microphone noise gate. Strength 0..=100
    /// maps to threshold dB (0 → -60 dB barely cuts, 100 → 0 dB cuts
    /// most of the signal — practical sweet spot is around 30–60).
    #[serde(rename = "set-mic-noise-gate")]
    SetMicNoiseGate { enabled: bool, strength: u8 },
    /// Mild RNNoise. Strength 0..=100 maps to a low VAD-threshold
    /// range (≤ 0.5) — meaningful suppression without the
    /// aggressive cuts that AI NC produces.
    #[serde(rename = "set-mic-noise-reduction")]
    SetMicNoiseReduction { enabled: bool, strength: u8 },
    /// Aggressive RNNoise. Strength 0..=100 maps to the full VAD-
    /// threshold range (0..0.95) — at maximum the plugin cuts almost
    /// everything but speech, including some quieter speech.
    #[serde(rename = "set-mic-ai-nc")]
    SetMicAiNoiseCancellation { enabled: bool, strength: u8 },
    /// Toggle / parameterise the Volume Stabilizer. Strength 0..=100
    /// maps to the chosen plugin's "more compression" parameter.
    /// `kind` selects which LADSPA plugin powers the stage; if
    /// omitted, the daemon keeps the previously-stored kind.
    #[serde(rename = "set-mic-volume-stabilizer")]
    SetMicVolumeStabilizer {
        enabled: bool,
        strength: u8,
        #[serde(default)]
        kind: Option<VolumeStabilizerKind>,
    },
    /// Set headset hardware sidetone level. 0..=128 normalised — the
    /// daemon maps to the device's 4-step internal setting and saves
    /// to EEPROM so it persists across power cycles.
    #[serde(rename = "set-sidetone")]
    SetSidetone { level: u8 },
    /// Toggle daemon-side desktop notifications (the connect /
    /// disconnect notify-send popups). Distinct from the GUI's own
    /// minimize-to-tray toast, which is GUI-side only.
    #[serde(rename = "set-notifications-enabled")]
    SetNotificationsEnabled { enabled: bool },
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
        // Boxed because EqState grew to 4 channels × 10 bands and would
        // otherwise dwarf every other variant of this enum (clippy's
        // large_enum_variant lint catches this). serde flattens
        // Box<EqState> to the same JSON shape as EqState, so the wire
        // contract is unchanged.
        eq_state: Box<EqState>,
        surround_enabled: bool,
        surround_hrir_path: Option<String>,
        mic_state: MicState,
        sidetone_level: u8,
        notifications_enabled: bool,
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

    /// Fired whenever any band on a channel changes — emits the full
    /// 10-band array for that channel so the GUI can refresh every
    /// slider + frequency label at once and stay in sync. Carries the
    /// FULL band parameters (not just gains) so preset loads update
    /// frequency labels too.
    #[serde(rename = "eq-bands-changed")]
    EqBandsChanged {
        channel: EqChannel,
        bands: [EqBand; NUM_BANDS],
    },

    /// Fired when surround insertion is toggled.
    #[serde(rename = "surround-enabled-changed")]
    SurroundEnabledChanged { enabled: bool },

    /// Fired when the HRIR file path changes (saved + applied or cleared).
    #[serde(rename = "surround-hrir-changed")]
    SurroundHrirChanged { path: Option<String> },

    /// Fired whenever any microphone-processing feature toggles or
    /// changes strength. The full MicState ships every time so the
    /// GUI doesn't have to track which feature changed — it just
    /// re-applies the snapshot.
    #[serde(rename = "mic-state-changed")]
    MicStateChanged { state: MicState },

    /// Fired when sidetone level changes (GUI command, persisted
    /// state restore, etc.). Carries the level the daemon stored,
    /// post-clamp.
    #[serde(rename = "sidetone-changed")]
    SidetoneChanged { level: u8 },

    /// Fired when daemon-side desktop notifications are toggled.
    #[serde(rename = "notifications-enabled-changed")]
    NotificationsEnabledChanged { enabled: bool },

    /// Fired when the daemon promotes / demotes SteelMic as the
    /// system default source. `active=true` means SteelMic is now
    /// the default; the GUI uses this to show a one-time
    /// notification explaining what just changed.
    #[serde(rename = "mic-default-source-changed")]
    MicDefaultSourceChanged { active: bool },
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
            eq_state: Box::new(EqState::default()),
            surround_enabled: false,
            surround_hrir_path: None,
            mic_state: MicState::default(),
            sidetone_level: 0,
            notifications_enabled: true,
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
        assert!(json["eq_state"]["game"].is_array());
        assert_eq!(
            json["eq_state"]["game"].as_array().unwrap().len(),
            NUM_BANDS
        );
        assert_eq!(json["surround_enabled"], false);
        assert!(json["surround_hrir_path"].is_null());
    }

    #[test]
    fn set_eq_band_gain_command_parses() {
        let cmd: ClientCommand =
            from_str(r#"{"cmd":"set-eq-band-gain","channel":"chat","band":3,"gain_db":-4.5}"#)
                .unwrap();
        match cmd {
            ClientCommand::SetEqBandGain {
                channel,
                band,
                gain_db,
            } => {
                assert_eq!(channel, EqChannel::Chat);
                assert_eq!(band, 3);
                assert!((gain_db - -4.5).abs() < 1e-6);
            }
            other => panic!("expected SetEqBandGain, got {other:?}"),
        }
    }

    #[test]
    fn set_eq_band_command_parses() {
        let cmd: ClientCommand = from_str(
            r#"{"cmd":"set-eq-band","channel":"game","band":4,
                "params":{"freq":250.0,"q":0.7071,"gain":5.0,
                          "type":"peaking","enabled":true}}"#,
        )
        .unwrap();
        match cmd {
            ClientCommand::SetEqBand {
                channel,
                band,
                params,
            } => {
                assert_eq!(channel, EqChannel::Game);
                assert_eq!(band, 4);
                assert!((params.freq - 250.0).abs() < 1e-3);
                assert!((params.gain - 5.0).abs() < 1e-3);
                assert_eq!(params.band_type, BandType::Peaking);
            }
            other => panic!("expected SetEqBand, got {other:?}"),
        }
    }

    #[test]
    fn eq_channel_accepts_media_and_hdmi() {
        for (raw, expected) in [
            (r#""game""#, EqChannel::Game),
            (r#""chat""#, EqChannel::Chat),
            (r#""media""#, EqChannel::Media),
            (r#""hdmi""#, EqChannel::Hdmi),
        ] {
            let parsed: EqChannel = from_str(raw).unwrap();
            assert_eq!(parsed, expected);
        }
    }

    #[test]
    fn eq_state_default_includes_media_and_hdmi() {
        let st = EqState::default();
        let json: Value = from_str(&to_string(&st).unwrap()).unwrap();
        for ch in ["game", "chat", "media", "hdmi"] {
            let arr = json[ch].as_array().expect(ch);
            assert_eq!(arr.len(), NUM_BANDS);
        }
    }

    #[test]
    fn set_eq_channel_command_parses() {
        let bands = default_channel_bands();
        let payload = format!(
            r#"{{"cmd":"set-eq-channel","channel":"chat","bands":{}}}"#,
            serde_json::to_string(&bands).unwrap()
        );
        let cmd: ClientCommand = from_str(&payload).unwrap();
        match cmd {
            ClientCommand::SetEqChannel {
                channel,
                bands: parsed,
            } => {
                assert_eq!(channel, EqChannel::Chat);
                assert_eq!(parsed.len(), NUM_BANDS);
                assert_eq!(parsed[0].band_type, BandType::Lowshelf);
                assert_eq!(parsed[NUM_BANDS - 1].band_type, BandType::Highshelf);
            }
            other => panic!("expected SetEqChannel, got {other:?}"),
        }
    }

    #[test]
    fn eq_bands_changed_event_shape() {
        let ev = DaemonEvent::EqBandsChanged {
            channel: EqChannel::Game,
            bands: default_channel_bands(),
        };
        let json: Value = from_str(&to_string(&ev).unwrap()).unwrap();
        assert_eq!(json["event"], "eq-bands-changed");
        assert_eq!(json["channel"], "game");
        assert_eq!(json["bands"].as_array().unwrap().len(), NUM_BANDS);
        assert_eq!(json["bands"][0]["freq"], 32.0);
        assert_eq!(json["bands"][0]["type"], "lowshelf");
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
    fn set_sidetone_command_parses() {
        let cmd: ClientCommand =
            from_str(r#"{"cmd":"set-sidetone","level":64}"#).unwrap();
        match cmd {
            ClientCommand::SetSidetone { level } => assert_eq!(level, 64),
            other => panic!("expected SetSidetone, got {other:?}"),
        }
    }

    #[test]
    fn set_notifications_enabled_command_parses() {
        let cmd: ClientCommand =
            from_str(r#"{"cmd":"set-notifications-enabled","enabled":false}"#).unwrap();
        match cmd {
            ClientCommand::SetNotificationsEnabled { enabled } => assert!(!enabled),
            other => panic!("expected SetNotificationsEnabled, got {other:?}"),
        }
    }

    #[test]
    fn set_mic_commands_parse() {
        let cmd: ClientCommand =
            from_str(r#"{"cmd":"set-mic-noise-gate","enabled":true,"strength":40}"#).unwrap();
        match cmd {
            ClientCommand::SetMicNoiseGate { enabled, strength } => {
                assert!(enabled);
                assert_eq!(strength, 40);
            }
            other => panic!("expected SetMicNoiseGate, got {other:?}"),
        }
        let cmd: ClientCommand =
            from_str(r#"{"cmd":"set-mic-ai-nc","enabled":false,"strength":80}"#).unwrap();
        match cmd {
            ClientCommand::SetMicAiNoiseCancellation { enabled, strength } => {
                assert!(!enabled);
                assert_eq!(strength, 80);
            }
            other => panic!("expected SetMicAiNoiseCancellation, got {other:?}"),
        }
    }

    #[test]
    fn mic_default_source_changed_event_shape() {
        let ev = DaemonEvent::MicDefaultSourceChanged { active: true };
        let json: Value = from_str(&to_string(&ev).unwrap()).unwrap();
        assert_eq!(json["event"], "mic-default-source-changed");
        assert_eq!(json["active"], true);
    }

    #[test]
    fn mic_state_changed_event_shape() {
        let ev = DaemonEvent::MicStateChanged {
            state: MicState {
                noise_gate: MicFeature {
                    enabled: true,
                    strength: 30,
                },
                noise_reduction: MicFeature::default(),
                ai_noise_cancellation: MicFeature {
                    enabled: true,
                    strength: 70,
                },
                volume_stabilizer: MicFeature::default(),
                volume_stabilizer_kind: VolumeStabilizerKind::default(),
            },
        };
        let json: Value = from_str(&to_string(&ev).unwrap()).unwrap();
        assert_eq!(json["event"], "mic-state-changed");
        assert_eq!(json["state"]["noise_gate"]["enabled"], true);
        assert_eq!(json["state"]["noise_gate"]["strength"], 30);
        assert_eq!(json["state"]["ai_noise_cancellation"]["strength"], 70);
    }

    #[test]
    fn reset_state_command_parses() {
        let cmd: ClientCommand = from_str(r#"{"cmd":"reset-state"}"#).unwrap();
        assert!(matches!(cmd, ClientCommand::ResetState));
    }

    #[test]
    fn set_surround_enabled_command_parses() {
        let cmd: ClientCommand =
            from_str(r#"{"cmd":"set-surround-enabled","enabled":true}"#).unwrap();
        match cmd {
            ClientCommand::SetSurroundEnabled { enabled } => assert!(enabled),
            other => panic!("expected SetSurroundEnabled, got {other:?}"),
        }
    }

    #[test]
    fn set_surround_hrir_command_parses_path_and_null() {
        let with_path: ClientCommand =
            from_str(r#"{"cmd":"set-surround-hrir","path":"/x/foo.wav"}"#).unwrap();
        match with_path {
            ClientCommand::SetSurroundHrir { path } => {
                assert_eq!(path.as_deref(), Some("/x/foo.wav"))
            }
            other => panic!("expected SetSurroundHrir, got {other:?}"),
        }
        let cleared: ClientCommand =
            from_str(r#"{"cmd":"set-surround-hrir","path":null}"#).unwrap();
        match cleared {
            ClientCommand::SetSurroundHrir { path } => assert!(path.is_none()),
            other => panic!("expected SetSurroundHrir, got {other:?}"),
        }
    }

    #[test]
    fn surround_event_shapes() {
        let on = DaemonEvent::SurroundEnabledChanged { enabled: true };
        let json: Value = from_str(&to_string(&on).unwrap()).unwrap();
        assert_eq!(json["event"], "surround-enabled-changed");
        assert_eq!(json["enabled"], true);
        let path_ev = DaemonEvent::SurroundHrirChanged {
            path: Some("/x/f.wav".into()),
        };
        let json: Value = from_str(&to_string(&path_ev).unwrap()).unwrap();
        assert_eq!(json["event"], "surround-hrir-changed");
        assert_eq!(json["path"], "/x/f.wav");
    }

    #[test]
    fn auto_route_browsers_changed_event_shape() {
        let ev = DaemonEvent::AutoRouteBrowsersChanged { enabled: true };
        let json: Value = from_str(&to_string(&ev).unwrap()).unwrap();
        assert_eq!(json["event"], "auto-route-browsers-changed");
        assert_eq!(json["enabled"], true);
    }
}
