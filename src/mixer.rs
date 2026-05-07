//! Core mixer logic: connect to device, create sinks, read dial events,
//! adjust volumes, and broadcast state to GUI clients.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use log::{debug, info, warn};

use crate::audio::{SinkManager, CHAT_SINK, GAME_SINK};
use crate::config;
use crate::display::ChatMixGauge;
use crate::hid::{BatteryStatus, HidEvent, NovaDevice};
use crate::protocol::{
    AncMode, DaemonEvent, EqChannel, EqState, MicGain, MicState, VolumeBoostState, WirelessMode,
};

pub type SharedSinks = Arc<Mutex<SinkManager>>;

/// Fan out an event to subscribed GUI clients. Free function so the socket
/// handler can broadcast from a client-response thread without going
/// through the Mixer struct (e.g. to echo a runtime media-sink toggle).
pub fn broadcast_event(
    subscribers: &Arc<Mutex<Vec<std::sync::mpsc::Sender<DaemonEvent>>>>,
    event: DaemonEvent,
) {
    let mut subs = subscribers.lock().unwrap();
    subs.retain(|tx| tx.send(event.clone()).is_ok());
}

/// Persist the dial-tracking part of MixerState to disk. Called from
/// the dial-event handler so the latest game/chat split survives a
/// daemon restart instead of resetting to 100/100. Cheap (one small
/// JSON write) and the dial fires at most a few events per session,
/// so per-event writes are fine. We pull every other field from the
/// current state so this stays consistent with main.rs's
/// persist_sink_state helper for non-dial commands.
fn persist_dial_state(state: &Arc<Mutex<MixerState>>) {
    let st = state.lock().unwrap();
    config::save(&config::DaemonState {
        media_sink_enabled: st.media_sink_enabled,
        hdmi_sink_enabled: st.hdmi_sink_enabled,
        auto_route_browsers: st.auto_route_browsers,
        eq_enabled: st.eq_enabled,
        eq_state: st.eq_state,
        surround_enabled: st.surround_enabled,
        surround_hrir_path: st.surround_hrir_path.clone(),
        mic_state: st.mic_state,
        mic_default_applied: true,
        sidetone_level: st.sidetone_level,
        notifications_enabled: st.notifications_enabled,
        volume_boost: st.volume_boost,
        game_vol: st.game_vol,
        chat_vol: st.chat_vol,
        oled_brightness: st.oled_brightness,
        anc_mode: st.anc_mode,
        anc_transparent_level: st.anc_transparent_level,
        wireless_mode: st.wireless_mode,
        mic_gain: st.mic_gain,
        mic_volume: st.mic_volume,
        mic_led_brightness: st.mic_led_brightness,
        deck_control_enabled: st.deck_control_enabled,
    });
}

/// After this many consecutive battery polls return no answer
/// (without an outright HID write error), assume the HID fd has gone
/// stale — usually post-system-suspend where hidapi still thinks the
/// fd is open but the kernel re-enumerated USB underneath us.
/// At BATTERY_POLL_INTERVAL=60s, this means ~3 minutes of silence
/// before forcing a reconnect, which leaves room for a one-off
/// firmware quirk to recover on the next poll.
const HID_WATCHDOG_FAIL_THRESHOLD: u32 = 3;

/// Outcome of a battery poll. Drives the post-suspend HID watchdog.
enum BatteryPollResult {
    Got,
    NoReply,
    WriteFailed,
}

const RECONNECT_BASE: Duration = Duration::from_secs(3);
const RECONNECT_MAX: Duration = Duration::from_secs(30);
const BATTERY_POLL_INTERVAL: Duration = Duration::from_secs(60);
// Watchdog cadence for the mic chain. After system suspend the
// spawned `pipewire -c <conf>` child can die when its capture-side
// ALSA source disappears, leaving the mic effectively dead until
// the user toggles a feature. 5s is short enough that a typical
// resume-from-sleep brings the mic back before the user reaches for
// their app, but long enough to not show up in profiling.
const MIC_HEALTH_INTERVAL: Duration = Duration::from_secs(5);
// Sink-graph health watchdog. PipeWire restarting (user reloads it,
// distro pushes a pipewire upgrade, wireplumber crashes) destroys
// every module-loaded null-sink we own without giving the daemon a
// signal — we'd silently lose SteelGame / SteelChat / SteelMedia /
// SteelHDMI / surround chain and the user just hears the headset's
// raw output bypassing everything. Polling pactl every 10s for
// SteelGame is cheap (~5 ms shell-out) and lets us rebuild the whole
// graph automatically. Slower than the mic watchdog because sink
// loss is less common — pipewire restart is a rare event whereas
// the mic chain dies routinely on suspend.
const SINK_HEALTH_INTERVAL: Duration = Duration::from_secs(10);

/// Why the event loop returned. Reconnect on `Disconnected`, exit on `Shutdown`.
enum SessionEnd {
    Disconnected,
    Shutdown,
}

/// Draw the gauge and, if the draw fails, drop the handle so we don't retry
/// on every dial event. Wireless firmware that rejects feature reports stays
/// rejecting them, and retrying costs ~970 ms per attempt inside ggoled_lib.
fn draw_or_drop(display: &mut Option<ChatMixGauge>, game: u8, chat: u8) {
    if let Some(ref mut d) = display {
        if !d.show(game, chat) {
            warn!("OLED gauge failing — disabling for this session");
            *display = None;
        }
    }
}

/// Shared mixer state accessible by the socket server.
pub struct MixerState {
    pub connected: bool,
    pub game_vol: u8,
    pub chat_vol: u8,
    pub battery: Option<BatteryStatus>,
    pub media_sink_enabled: bool,
    pub hdmi_sink_enabled: bool,
    pub auto_route_browsers: bool,
    pub eq_enabled: bool,
    pub eq_state: EqState,
    pub surround_enabled: bool,
    pub surround_hrir_path: Option<std::path::PathBuf>,
    pub mic_state: MicState,
    pub sidetone_level: u8,
    pub notifications_enabled: bool,
    /// Per-channel digital volume multiplier applied at the
    /// pactl set-sink-volume call site. Scales the chatmix-derived
    /// game/chat volume and the fixed 100% volume on Media/HDMI.
    pub volume_boost: VolumeBoostState,
    /// Snapshot of the chatmix dial value at the moment a channel's
    /// boost was last toggled ON, used to restore the user's pre-
    /// boost balance when the boost is later turned OFF. Without
    /// this, lowering the dial during boost (to compensate for the
    /// extra loudness) and then disabling boost would leave the sink
    /// stuck at the now-too-low dial reading. Transient — never
    /// persisted across daemon restarts. None means "not currently
    /// holding a snapshot for this channel"; some(v) means "boost is
    /// currently on for this channel and the dial-at-enable was v".
    pub pre_boost_game_vol: Option<u8>,
    pub pre_boost_chat_vol: Option<u8>,
    /// Persisted OLED brightness (1..=10). Re-applied on every
    /// reconnect — firmware does not remember this across power cycles.
    pub oled_brightness: u8,
    /// Last brightness value pushed to the device; mismatch with
    /// `oled_brightness` triggers a re-send in `event_loop`.
    pub applied_oled_brightness: u8,
    /// True iff the connected hardware exposes an OLED. Lets the GUI
    /// gate OLED controls instead of assuming connected == has OLED.
    pub oled_present: bool,
    /// Headset ANC mode. Persisted across reconnects; re-applied when
    /// the base station comes back. Hardware-button presses on the
    /// headset push back via OPT_NOISE_CANCELLING events; we mirror
    /// those into state without re-sending.
    pub anc_mode: AncMode,
    /// Transparent-mode intensity level (1..=10). Persisted.
    pub anc_transparent_level: u8,
    /// Last-pushed ANC-mode byte; mismatch with `anc_mode` triggers a
    /// re-send in `event_loop`.
    pub applied_anc_mode: u8,
    /// Last-pushed transparent level; same pattern.
    pub applied_anc_transparent_level: u8,
    /// 2.4 GHz wireless mode. Persisted; re-applied on reconnect when
    /// the device-side value differs (battery poll syncs that).
    pub wireless_mode: WirelessMode,
    /// Last-pushed wireless_mode byte. Sentinel-init means the first
    /// event-loop iteration always pushes — but only if it differs
    /// from what the device reports back via the battery poll mirror,
    /// preventing unnecessary radio bounces on reconnect.
    pub applied_wireless_mode: u8,
    /// Audio gain (Low/High). Persisted; re-applied on reconnect.
    pub mic_gain: MicGain,
    pub applied_mic_gain: u8,
    /// Mic volume (1..=10). Persisted; re-applied on reconnect.
    pub mic_volume: u8,
    pub applied_mic_volume: u8,
    /// Mic-mute LED brightness (1..=10). Persisted.
    pub mic_led_brightness: u8,
    pub applied_mic_led_brightness: u8,
    /// Master switch: when false, the daemon never writes deck-side
    /// settings to the device — pure-observer mode for users who
    /// already configure the headset via SteelSeries GG / hardware
    /// buttons. Reads from the device (battery-poll mirror) continue
    /// regardless so the GUI still tracks reality.
    pub deck_control_enabled: bool,
}

impl MixerState {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        media_sink_enabled: bool,
        hdmi_sink_enabled: bool,
        auto_route_browsers: bool,
        eq_enabled: bool,
        eq_state: EqState,
        surround_enabled: bool,
        surround_hrir_path: Option<std::path::PathBuf>,
        mic_state: MicState,
        sidetone_level: u8,
        notifications_enabled: bool,
        volume_boost: VolumeBoostState,
        game_vol: u8,
        chat_vol: u8,
        oled_brightness: u8,
        anc_mode: AncMode,
        anc_transparent_level: u8,
        wireless_mode: WirelessMode,
        mic_gain: MicGain,
        mic_volume: u8,
        mic_led_brightness: u8,
        deck_control_enabled: bool,
    ) -> Self {
        MixerState {
            connected: false,
            game_vol,
            chat_vol,
            battery: None,
            media_sink_enabled,
            hdmi_sink_enabled,
            auto_route_browsers,
            eq_enabled,
            eq_state,
            surround_enabled,
            surround_hrir_path,
            mic_state,
            sidetone_level,
            notifications_enabled,
            volume_boost,
            pre_boost_game_vol: None,
            pre_boost_chat_vol: None,
            oled_brightness,
            applied_oled_brightness: 0,
            oled_present: false,
            anc_mode,
            anc_transparent_level,
            applied_anc_mode: u8::MAX,
            applied_anc_transparent_level: 0,
            wireless_mode,
            applied_wireless_mode: u8::MAX,
            mic_gain,
            applied_mic_gain: u8::MAX,
            mic_volume,
            applied_mic_volume: 0,
            mic_led_brightness,
            applied_mic_led_brightness: 0,
            deck_control_enabled,
        }
    }
}

/// The core mixer. Runs in its own thread, broadcasting events to subscribers.
pub struct Mixer {
    running: Arc<AtomicBool>,
    state: Arc<Mutex<MixerState>>,
    subscribers: Arc<Mutex<Vec<std::sync::mpsc::Sender<DaemonEvent>>>>,
    sinks: SharedSinks,
    notify_enabled: bool,
    notify_available: bool,
    /// True iff the libusb hotplug watcher last saw the Nova Pro
    /// Wireless on the bus. The event loop polls this each iteration
    /// so a USB-level disconnect (typical on PC suspend/resume) shows
    /// up instantly instead of waiting on the battery-poll watchdog.
    device_present: Arc<AtomicBool>,
}

impl Mixer {
    pub fn new(
        running: Arc<AtomicBool>,
        state: Arc<Mutex<MixerState>>,
        subscribers: Arc<Mutex<Vec<std::sync::mpsc::Sender<DaemonEvent>>>>,
        sinks: SharedSinks,
        notify_enabled: bool,
        device_present: Arc<AtomicBool>,
    ) -> Self {
        // Probe notify-send once. Missing on headless servers and some
        // minimal DEs — we skip silently rather than spawning a failing
        // subprocess for every event.
        let notify_available = notify_enabled
            && std::process::Command::new("notify-send")
                .arg("--version")
                .stdout(std::process::Stdio::null())
                .stderr(std::process::Stdio::null())
                .status()
                .map(|s| s.success())
                .unwrap_or(false);
        if notify_enabled && !notify_available {
            warn!(
                "notify-send not available — desktop notifications disabled \
                 (install libnotify / libnotify-bin to enable)"
            );
        }

        Mixer {
            running,
            state,
            subscribers,
            sinks,
            notify_enabled,
            notify_available,
            device_present,
        }
    }

    /// Broadcast an event to all subscribed GUI clients. Removes dead senders.
    fn broadcast(&self, event: DaemonEvent) {
        broadcast_event(&self.subscribers, event);
    }

    /// Mirror device-reported mic LED brightness into MixerState.
    /// Used so the GUI tracks whatever value the device actually has
    /// after suspend/reconnect or after another tool changes it.
    fn sync_mic_led_from_device(&self, level: u8) {
        let clamped = level.clamp(1, 10);
        let changed = {
            let mut st = self.state.lock().unwrap();
            let prior = st.mic_led_brightness;
            st.mic_led_brightness = clamped;
            st.applied_mic_led_brightness = clamped;
            prior != clamped
        };
        if changed {
            persist_dial_state(&self.state);
            self.broadcast(DaemonEvent::MicLedBrightnessChanged { level: clamped });
        }
    }

    /// Mirror device-reported wireless_mode into MixerState. Called
    /// from the battery-poll path so that on reconnect, if the device
    /// already happens to be in the persisted mode (or some other
    /// tool changed it), we don't unnecessarily re-write and bounce
    /// the radio.
    fn sync_wireless_from_device(&self, wireless_byte: u8) {
        let new_mode = WirelessMode::from_byte(wireless_byte);
        let changed = {
            let mut st = self.state.lock().unwrap();
            let prior = st.wireless_mode;
            st.wireless_mode = new_mode;
            // Lockstep: marks applied so the event loop won't try to
            // push back to the device.
            st.applied_wireless_mode = new_mode.as_byte();
            prior != new_mode
        };
        if changed {
            persist_dial_state(&self.state);
            self.broadcast(DaemonEvent::WirelessModeChanged { mode: new_mode });
        }
    }

    /// Mirror device-reported ANC state into MixerState without
    /// re-pushing to the device. Called when the periodic battery
    /// reply carries fresh ANC bytes — covers the case where the
    /// user presses the headset's ANC button while we weren't
    /// listening for an OPT_NOISE_CANCELLING push event.
    fn sync_anc_from_device(&self, anc_byte: u8, trans_level: u8) {
        let new_mode = AncMode::from_byte(anc_byte);
        let new_trans = trans_level.clamp(1, 10);
        let (mode_changed, trans_changed) = {
            let mut st = self.state.lock().unwrap();
            let mode_changed = st.anc_mode != new_mode;
            let trans_changed = st.anc_transparent_level != new_trans;
            st.anc_mode = new_mode;
            st.anc_transparent_level = new_trans;
            st.applied_anc_mode = new_mode.as_byte();
            st.applied_anc_transparent_level = new_trans;
            (mode_changed, trans_changed)
        };
        if mode_changed || trans_changed {
            persist_dial_state(&self.state);
        }
        if mode_changed {
            self.broadcast(DaemonEvent::AncModeChanged { mode: new_mode });
        }
        if trans_changed {
            self.broadcast(DaemonEvent::AncTransparentLevelChanged {
                level: new_trans,
            });
        }
    }

    /// Send a desktop notification via notify-send. Two gates:
    ///   - `notify_available`: notify-send is installed (probed once
    ///     at construction; doesn't change at runtime).
    ///   - `notify_enabled` constructor arg: the `--no-notify` CLI
    ///     flag, hard-disable for the whole daemon lifetime.
    ///   - `MixerState.notifications_enabled`: runtime user toggle
    ///     from the GUI Settings tab — read on every call so the
    ///     user can flip it without restarting the daemon.
    fn notify(&self, summary: &str, body: &str) {
        if !self.notify_enabled || !self.notify_available {
            return;
        }
        let runtime_on = self.state.lock().unwrap().notifications_enabled;
        if !runtime_on {
            return;
        }
        let mut cmd = std::process::Command::new("notify-send");
        cmd.args(["-a", "steelvoicemix", "-i", "audio-headset", summary]);
        if !body.is_empty() {
            cmd.arg(body);
        }
        let _ = cmd
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .spawn();
    }

    /// Main run loop. Blocks until `running` is set to false.
    pub fn run(&mut self) {
        info!(
            "steelvoicemix v{} starting (notify={}, RUST_LOG={})",
            env!("CARGO_PKG_VERSION"),
            self.notify_enabled,
            std::env::var("RUST_LOG").unwrap_or_else(|_| "info".into()),
        );
        let mut reconnect_wait = RECONNECT_BASE;

        while self.running.load(Ordering::Relaxed) {
            let (dev, output_sink) = match self.attempt_connect() {
                Some(conn) => {
                    reconnect_wait = RECONNECT_BASE;
                    conn
                }
                None => {
                    self.wait(reconnect_wait);
                    reconnect_wait = (reconnect_wait * 2).min(RECONNECT_MAX);
                    continue;
                }
            };

            match self.run_session(dev, output_sink) {
                SessionEnd::Disconnected => {
                    self.notify("🎧 Base Station Disconnected", "Waiting for reconnect...");
                    if self.running.load(Ordering::Relaxed) {
                        info!("Will attempt reconnect...");
                    }
                }
                SessionEnd::Shutdown => {}
            }
        }

        info!("steelvoicemix stopped");
    }

    /// Find the headset PipeWire sink and open the HID interface. `None` means
    /// "not ready yet, back off and try again."
    fn attempt_connect(&self) -> Option<(NovaDevice, String)> {
        let output_sink = match SinkManager::find_output_sink() {
            Some(s) => {
                info!("Detected headset output sink: {s}");
                s
            }
            None => {
                warn!("Output sink not found — is the headset connected?");
                return None;
            }
        };

        let dev = match NovaDevice::open() {
            Ok(d) => d,
            Err(e) => {
                warn!("{e} — waiting...");
                return None;
            }
        };

        if let Err(e) = dev.enable_chatmix() {
            warn!("Lost connection during setup: {e}");
            return None;
        }
        info!("Base station connected, ChatMix enabled");

        // Push the persisted sidetone level to the device on connect
        // (only when deck control is enabled). The headset's EEPROM
        // remembers across power cycles, but we re-send anyway in
        // case the user's been on a different machine since.
        let (level, deck_control_enabled) = {
            let st = self.state.lock().unwrap();
            (st.sidetone_level, st.deck_control_enabled)
        };
        if deck_control_enabled {
            if let Err(e) = dev.set_sidetone(level) {
                warn!("Could not restore sidetone level {level}: {e}");
            }
        }

        Some((dev, output_sink))
    }

    /// Everything that happens while the base station is connected: set up
    /// virtual sinks and OLED, announce state, and run the event loop until
    /// the user unplugs or we're told to shut down.
    fn run_session(&mut self, dev: NovaDevice, output_sink: String) -> SessionEnd {
        // Master gate: passive-open the OLED (no brightness write,
        // no draw probe) when the user has deck control disabled.
        // The handle still exists so oled_present is true and the
        // GUI shows the Deck tab; controls are greyed-out client-side.
        let (initial_brightness, deck_control_enabled) = {
            let st = self.state.lock().unwrap();
            (st.oled_brightness, st.deck_control_enabled)
        };
        let (mut display, present_now) = if deck_control_enabled {
            match ChatMixGauge::new(initial_brightness) {
                Ok(d) => {
                    info!(
                        "OLED display initialized at brightness {initial_brightness} (gauge {})",
                        if d.can_draw() { "enabled" } else { "disabled" }
                    );
                    (Some(d), true)
                }
                Err(e) => {
                    warn!("OLED display not available: {e}");
                    (None, false)
                }
            }
        } else {
            match ChatMixGauge::open_passive() {
                Ok(d) => (Some(d), true),
                Err(e) => {
                    warn!("OLED display not available (passive open): {e}");
                    (None, false)
                }
            }
        };
        let was_present = {
            let mut st = self.state.lock().unwrap();
            let prior = st.oled_present;
            st.oled_present = present_now;
            if present_now {
                st.applied_oled_brightness = initial_brightness;
            }
            prior
        };
        if was_present != present_now {
            self.broadcast(DaemonEvent::OledPresenceChanged { present: present_now });
        }

        {
            let mut sinks = self.sinks.lock().unwrap();
            sinks.create_sinks(&output_sink);
        }

        let (init_game, init_chat) = self.resolve_initial_dial(&dev);
        let (game_boost, chat_boost) = {
            let st = self.state.lock().unwrap();
            (
                st.volume_boost.for_channel(EqChannel::Game),
                st.volume_boost.for_channel(EqChannel::Chat),
            )
        };
        SinkManager::set_volume(GAME_SINK, game_boost.apply(init_game));
        SinkManager::set_volume(CHAT_SINK, chat_boost.apply(init_chat));

        {
            let mut st = self.state.lock().unwrap();
            st.connected = true;
            st.game_vol = init_game;
            st.chat_vol = init_chat;
        }

        self.broadcast(DaemonEvent::Connected);
        self.broadcast(DaemonEvent::ChatMix {
            game: init_game,
            chat: init_chat,
        });
        draw_or_drop(&mut display, init_game, init_chat);
        let (media_live, hdmi_live) = {
            let sm = self.sinks.lock().unwrap();
            (sm.media_enabled(), sm.hdmi_enabled())
        };
        let mut active_sinks: Vec<&str> = vec!["SteelGame", "SteelChat"];
        if media_live {
            active_sinks.push("SteelMedia");
        }
        if hdmi_live {
            active_sinks.push("SteelHDMI");
        }
        let notify_body = if media_live || hdmi_live {
            format!(
                "{} sinks ready.\nUse the dial to balance Game vs Chat — Media and HDMI stay independent.",
                active_sinks.join(", ")
            )
        } else {
            "SteelGame and SteelChat sinks ready.\nUse the dial to control balance.".to_string()
        };
        self.notify("🎧 ChatMix Active", &notify_body);

        let _ = self.poll_and_broadcast_battery(&dev);

        info!("Listening for ChatMix dial events...");
        let end = self.event_loop(&dev, &mut display);

        // Teardown
        if let Some(ref mut d) = display {
            d.clear();
        }
        drop(display);
        let _ = dev.disable_chatmix();
        {
            let mut sinks = self.sinks.lock().unwrap();
            sinks.destroy_sinks();
        }
        let was_present = {
            let mut st = self.state.lock().unwrap();
            st.connected = false;
            let was_present = st.oled_present;
            st.oled_present = false;
            was_present
        };
        if was_present {
            self.broadcast(DaemonEvent::OledPresenceChanged { present: false });
        }
        self.broadcast(DaemonEvent::Disconnected);

        end
    }

    /// Inner event pump — reads HID events, forwards volume changes, polls
    /// battery on idle timeouts. Returns whichever ended the session first:
    /// a disconnect or a shutdown signal.
    fn event_loop(
        &self,
        dev: &NovaDevice,
        display: &mut Option<ChatMixGauge>,
    ) -> SessionEnd {
        let mut last_battery_poll = Instant::now();
        let mut last_mic_health = Instant::now();
        let mut last_sink_health = Instant::now();
        // Counts consecutive battery polls that returned no answer.
        // After HID_WATCHDOG_FAIL_THRESHOLD silent polls (or one
        // outright write error) we force a reconnect — the kernel
        // typically re-enumerated the device under suspend without
        // hidapi noticing.
        let mut consecutive_failed_polls: u32 = 0;
        // Track the last-applied sidetone level so we can detect
        // GUI-driven changes from MixerState and push them to the
        // device. Initialised from current state (already applied at
        // connect) so the first iteration doesn't double-send.
        let mut last_sidetone = self.state.lock().unwrap().sidetone_level;

        while self.running.load(Ordering::Relaxed) {
            // Hotplug-driven fast disconnect. libusb just told us the
            // device left the bus (typical on PC suspend/resume),
            // so don't waste 3 minutes waiting for the watchdog.
            if !self.device_present.load(Ordering::Relaxed) {
                info!("USB hotplug: device left the bus, ending session");
                return SessionEnd::Disconnected;
            }
            // One state read per iteration covers all pending GUI
            // writes (sidetone + OLED brightness + ANC + wireless +
            // mic gain/volume/LED) + the master deck-control gate.
            let snap = {
                let st = self.state.lock().unwrap();
                (
                    st.sidetone_level,
                    st.oled_brightness,
                    st.applied_oled_brightness,
                    st.anc_mode,
                    st.applied_anc_mode,
                    st.anc_transparent_level,
                    st.applied_anc_transparent_level,
                    st.wireless_mode,
                    st.applied_wireless_mode,
                    st.mic_gain,
                    st.applied_mic_gain,
                    st.mic_volume,
                    st.applied_mic_volume,
                    st.mic_led_brightness,
                    st.applied_mic_led_brightness,
                    st.deck_control_enabled,
                )
            };
            let (
                want_sidetone,
                want_bright,
                applied_bright,
                want_anc_mode,
                applied_anc_mode,
                want_anc_trans,
                applied_anc_trans,
                want_wireless,
                applied_wireless,
                want_mic_gain,
                applied_mic_gain,
                want_mic_vol,
                applied_mic_vol,
                want_mic_led,
                applied_mic_led,
                deck_control_enabled,
            ) = snap;
            // All device writes are gated on the master deck-control
            // toggle. State still mutates freely client-side (so the
            // GUI can preview future changes), but nothing reaches
            // the firmware until the user opts in.
            if deck_control_enabled {
                if want_sidetone != last_sidetone {
                    if let Err(e) = dev.set_sidetone(want_sidetone) {
                        warn!("Failed to apply sidetone {want_sidetone}: {e}");
                    } else {
                        last_sidetone = want_sidetone;
                    }
                }

                // Mark applied unconditionally — on failure the firmware
                // is silently dropping writes; retrying every 100 ms would
                // spam HID + warn logs forever. Next reconnect re-pushes
                // via run_session.
                if want_bright != applied_bright {
                    if let Some(ref d) = display {
                        d.set_brightness(want_bright);
                    }
                    self.state.lock().unwrap().applied_oled_brightness = want_bright;
                }
            }

            // Apply pending ANC state (same gate). Same once-per-iter
            // mark-applied pattern: write, then mark applied
            // unconditionally so a silently-failed firmware write
            // doesn't loop forever.
            if deck_control_enabled {
                if want_anc_mode.as_byte() != applied_anc_mode {
                    if let Err(e) = dev.set_anc_mode(want_anc_mode.as_byte()) {
                        warn!("Failed to apply ANC mode {want_anc_mode:?}: {e}");
                    }
                    self.state.lock().unwrap().applied_anc_mode = want_anc_mode.as_byte();
                }
                if want_anc_trans != applied_anc_trans {
                    if let Err(e) = dev.set_anc_transparent_level(want_anc_trans) {
                        warn!("Failed to apply ANC transparent level {want_anc_trans}: {e}");
                    }
                    self.state.lock().unwrap().applied_anc_transparent_level = want_anc_trans;
                }
            }

            if deck_control_enabled {
                // Wireless-mode write briefly drops the radio link, so we
                // only push when the desired byte differs from what the
                // device reported back (battery-poll mirror keeps applied
                // in sync). Otherwise spamming set/toggle from a shortcut
                // would bounce the headset every keypress.
                if want_wireless.as_byte() != applied_wireless {
                    info!(
                        "Wireless mode change: {:?} → {:?} (radio will briefly drop link)",
                        WirelessMode::from_byte(applied_wireless),
                        want_wireless,
                    );
                    if let Err(e) = dev.set_wireless_mode(want_wireless.as_byte()) {
                        warn!("Failed to apply wireless mode {want_wireless:?}: {e}");
                    }
                    self.state.lock().unwrap().applied_wireless_mode = want_wireless.as_byte();
                }

                // Mic gain / volume / LED brightness — same once-per-iter
                // mark-applied-unconditionally pattern. None of these
                // disconnect the device on write, so no compare-and-skip
                // safety needed at the daemon-command layer.
                if want_mic_gain.as_byte() != applied_mic_gain {
                    debug!("mic gain → {:?}", want_mic_gain);
                    if let Err(e) = dev.set_mic_gain(want_mic_gain.as_byte()) {
                        warn!("Failed to apply mic gain {want_mic_gain:?}: {e}");
                    }
                    self.state.lock().unwrap().applied_mic_gain = want_mic_gain.as_byte();
                }
                if want_mic_vol != applied_mic_vol {
                    debug!("mic volume → {want_mic_vol}");
                    if let Err(e) = dev.set_mic_volume(want_mic_vol) {
                        warn!("Failed to apply mic volume {want_mic_vol}: {e}");
                    }
                    self.state.lock().unwrap().applied_mic_volume = want_mic_vol;
                }
                if want_mic_led != applied_mic_led {
                    debug!("mic LED brightness → {want_mic_led}");
                    if let Err(e) = dev.set_mic_led_brightness(want_mic_led) {
                        warn!("Failed to apply mic LED brightness {want_mic_led}: {e}");
                    }
                    self.state.lock().unwrap().applied_mic_led_brightness = want_mic_led;
                }
            }

            // Short timeout keeps dial-to-update latency low; battery
            // polling still triggers every BATTERY_POLL_INTERVAL.
            match dev.read(100) {
                Ok(Some(msg)) => {
                    // Any successful read proves the fd is alive —
                    // reset the post-suspend watchdog counter so a
                    // single one-off silent battery poll doesn't
                    // accumulate toward a false-positive disconnect.
                    consecutive_failed_polls = 0;
                    match NovaDevice::parse_event(&msg) {
                    HidEvent::ChatMix { game_vol, chat_vol } => {
                        debug!("dial: game={game_vol}% chat={chat_vol}%");
                        let (game_boost, chat_boost) = {
                            let st = self.state.lock().unwrap();
                            (
                                st.volume_boost.for_channel(EqChannel::Game),
                                st.volume_boost.for_channel(EqChannel::Chat),
                            )
                        };
                        SinkManager::set_volume(GAME_SINK, game_boost.apply(game_vol));
                        SinkManager::set_volume(CHAT_SINK, chat_boost.apply(chat_vol));
                        {
                            let mut st = self.state.lock().unwrap();
                            st.game_vol = game_vol;
                            st.chat_vol = chat_vol;
                        }
                        // Persist so a future daemon restart doesn't reset
                        // the split to defaults when the firmware doesn't
                        // answer get_chatmix.
                        persist_dial_state(&self.state);
                        // Broadcast to GUI/overlay first — the OLED draw
                        // can stall on firmware that rejects feature
                        // reports, and we don't want GUI updates waiting
                        // for it.
                        self.broadcast(DaemonEvent::ChatMix {
                            game: game_vol,
                            chat: chat_vol,
                        });
                        draw_or_drop(display, game_vol, chat_vol);
                    }
                    HidEvent::Battery(bat) => {
                        debug!("battery: {}% ({})", bat.level, bat.status);
                        // Battery reply also carries current ANC state per
                        // ASM yaml — mirror it into our state so the GUI
                        // tracks reality (e.g. user pressed the headset's
                        // ANC button while we weren't looking).
                        if let Some((anc_byte, trans_level)) =
                            NovaDevice::anc_from_battery_reply(&msg)
                        {
                            self.sync_anc_from_device(anc_byte, trans_level);
                        }
                        if let Some(wireless_byte) =
                            NovaDevice::wireless_mode_from_battery_reply(&msg)
                        {
                            self.sync_wireless_from_device(wireless_byte);
                        }
                        {
                            let mut st = self.state.lock().unwrap();
                            st.battery = Some(bat.clone());
                        }
                        self.broadcast(DaemonEvent::Battery {
                            level: bat.level,
                            status: bat.status,
                        });
                    }
                    HidEvent::AncMode(byte) => {
                        debug!("anc-mode push: byte=0x{byte:02x}");
                        let mode = AncMode::from_byte(byte);
                        let changed = {
                            let mut st = self.state.lock().unwrap();
                            let prior = st.anc_mode;
                            st.anc_mode = mode;
                            // Mark applied in lockstep so the event
                            // loop doesn't echo this back to the
                            // device on the next iteration.
                            st.applied_anc_mode = mode.as_byte();
                            prior != mode
                        };
                        if changed {
                            persist_dial_state(&self.state);
                            self.broadcast(DaemonEvent::AncModeChanged { mode });
                        }
                    }
                    HidEvent::AncTransparentLevel(level) => {
                        debug!("anc-transparent-level push: {level}");
                        let clamped = level.clamp(1, 10);
                        let changed = {
                            let mut st = self.state.lock().unwrap();
                            let prior = st.anc_transparent_level;
                            st.anc_transparent_level = clamped;
                            st.applied_anc_transparent_level = clamped;
                            prior != clamped
                        };
                        if changed {
                            persist_dial_state(&self.state);
                            self.broadcast(DaemonEvent::AncTransparentLevelChanged {
                                level: clamped,
                            });
                        }
                    }
                    HidEvent::Unknown => {}
                    }
                }
                Ok(None) => {
                    if last_battery_poll.elapsed() >= BATTERY_POLL_INTERVAL {
                        match self.poll_and_broadcast_battery(dev) {
                            BatteryPollResult::Got => {
                                consecutive_failed_polls = 0;
                            }
                            BatteryPollResult::NoReply => {
                                consecutive_failed_polls += 1;
                                warn!(
                                    "Battery poll {}/{}: no reply from device — possible stale HID fd post-suspend",
                                    consecutive_failed_polls,
                                    HID_WATCHDOG_FAIL_THRESHOLD,
                                );
                                if consecutive_failed_polls >= HID_WATCHDOG_FAIL_THRESHOLD {
                                    warn!(
                                        "HID watchdog: {} consecutive silent polls, forcing reconnect",
                                        consecutive_failed_polls,
                                    );
                                    return SessionEnd::Disconnected;
                                }
                            }
                            BatteryPollResult::WriteFailed => {
                                warn!("HID write failed during battery poll — forcing reconnect (definitive sign of stale fd)");
                                return SessionEnd::Disconnected;
                            }
                        }
                        last_battery_poll = Instant::now();
                    }
                    if last_mic_health.elapsed() >= MIC_HEALTH_INTERVAL {
                        self.sinks.lock().unwrap().check_mic_health();
                        last_mic_health = Instant::now();
                    }
                    if last_sink_health.elapsed() >= SINK_HEALTH_INTERVAL {
                        let respawned =
                            self.sinks.lock().unwrap().check_sinks_alive();
                        if respawned {
                            // Push the current dial value back through
                            // the freshly-rebuilt sinks so the user's
                            // game/chat split is right immediately
                            // (rather than waiting for the next dial
                            // event). volume_boost gets re-applied by
                            // the same code path the dial handler uses.
                            let (game_vol, chat_vol, game_boost, chat_boost) = {
                                let st = self.state.lock().unwrap();
                                (
                                    st.game_vol,
                                    st.chat_vol,
                                    st.volume_boost.for_channel(EqChannel::Game),
                                    st.volume_boost.for_channel(EqChannel::Chat),
                                )
                            };
                            SinkManager::set_volume(
                                GAME_SINK,
                                game_boost.apply(game_vol),
                            );
                            SinkManager::set_volume(
                                CHAT_SINK,
                                chat_boost.apply(chat_vol),
                            );
                            // Notify subscribed GUI clients so any
                            // downstream UI re-syncs (sink-graph state,
                            // overlay) after the rebuild.
                            self.broadcast(DaemonEvent::ChatMix {
                                game: game_vol,
                                chat: chat_vol,
                            });
                        }
                        last_sink_health = Instant::now();
                    }
                }
                Err(_) => {
                    warn!("Device disconnected");
                    return SessionEnd::Disconnected;
                }
            }
        }
        SessionEnd::Shutdown
    }

    /// Query the base station for the dial position, falling back to the
    /// last-known value so a brief unplug doesn't overwrite a user setting.
    fn resolve_initial_dial(&self, dev: &NovaDevice) -> (u8, u8) {
        match dev.get_chatmix() {
            Ok(Some(v)) => {
                info!("Initial dial position: game={}% chat={}%", v.0, v.1);
                v
            }
            _ => {
                let st = self.state.lock().unwrap();
                let fallback = (st.game_vol, st.chat_vol);
                info!(
                    "Dial position query silent — using last-known {}%/{}%",
                    fallback.0, fallback.1
                );
                fallback
            }
        }
    }

    /// Result of a battery poll, used by the event-loop watchdog to
    /// detect post-suspend stale HID fds. `Got` means the device
    /// answered; `NoReply` means request went out but no answer (may
    /// be a transient firmware quirk); `WriteFailed` means the HID
    /// write itself errored (definitive sign the fd is dead).
    fn poll_and_broadcast_battery(&self, dev: &NovaDevice) -> BatteryPollResult {
        match dev.get_battery() {
            Err(_) => BatteryPollResult::WriteFailed,
            Ok(None) => BatteryPollResult::NoReply,
            Ok(Some((bat, msg))) => {
                if let Some((anc_byte, trans_level)) =
                    NovaDevice::anc_from_battery_reply(&msg)
                {
                    self.sync_anc_from_device(anc_byte, trans_level);
                }
                if let Some(wireless_byte) =
                    NovaDevice::wireless_mode_from_battery_reply(&msg)
                {
                    self.sync_wireless_from_device(wireless_byte);
                }
                if let Some(led) =
                    NovaDevice::mic_led_brightness_from_battery_reply(&msg)
                {
                    self.sync_mic_led_from_device(led);
                }
                {
                    let mut st = self.state.lock().unwrap();
                    st.battery = Some(bat.clone());
                }
                self.broadcast(DaemonEvent::Battery {
                    level: bat.level,
                    status: bat.status,
                });
                BatteryPollResult::Got
            }
        }
    }

    /// Wait for a duration, checking `running` every second. Bails
    /// out early if the hotplug watcher reports the device returned
    /// to the bus mid-wait — turns post-suspend reconnect from
    /// "wait the full backoff window" into "retry as soon as the
    /// kernel re-enumerates the device".
    fn wait(&self, duration: Duration) {
        let start = Instant::now();
        while self.running.load(Ordering::Relaxed) && start.elapsed() < duration {
            if self.device_present.load(Ordering::Relaxed) {
                info!("USB hotplug: device back on bus during reconnect wait — retrying immediately");
                return;
            }
            thread::sleep(Duration::from_secs(1));
        }
    }
}
