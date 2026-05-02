//! Core mixer logic: connect to device, create sinks, read dial events,
//! adjust volumes, and broadcast state to GUI clients.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use log::{debug, info, warn};

use crate::audio::{SinkManager, CHAT_SINK, GAME_SINK};
use crate::display::ChatMixGauge;
use crate::hid::{BatteryStatus, HidEvent, NovaDevice};
use crate::protocol::{DaemonEvent, EqChannel, EqState, MicState, VolumeBoostState};

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
    ) -> Self {
        MixerState {
            connected: false,
            game_vol: 100,
            chat_vol: 100,
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
}

impl Mixer {
    pub fn new(
        running: Arc<AtomicBool>,
        state: Arc<Mutex<MixerState>>,
        subscribers: Arc<Mutex<Vec<std::sync::mpsc::Sender<DaemonEvent>>>>,
        sinks: SharedSinks,
        notify_enabled: bool,
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
        }
    }

    /// Broadcast an event to all subscribed GUI clients. Removes dead senders.
    fn broadcast(&self, event: DaemonEvent) {
        broadcast_event(&self.subscribers, event);
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

        // Push the persisted sidetone level to the device on connect.
        // The headset's EEPROM remembers across power cycles, but we
        // re-send anyway in case the user's been on a different
        // machine since.
        let level = self.state.lock().unwrap().sidetone_level;
        if let Err(e) = dev.set_sidetone(level) {
            warn!("Could not restore sidetone level {level}: {e}");
        }

        Some((dev, output_sink))
    }

    /// Everything that happens while the base station is connected: set up
    /// virtual sinks and OLED, announce state, and run the event loop until
    /// the user unplugs or we're told to shut down.
    fn run_session(&mut self, dev: NovaDevice, output_sink: String) -> SessionEnd {
        let mut display = match ChatMixGauge::new() {
            Ok(d) => {
                info!("OLED display initialized");
                Some(d)
            }
            Err(e) => {
                warn!("OLED display not available: {e}");
                None
            }
        };

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

        self.poll_and_broadcast_battery(&dev);

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
        {
            let mut st = self.state.lock().unwrap();
            st.connected = false;
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
        // Track the last-applied sidetone level so we can detect
        // GUI-driven changes from MixerState and push them to the
        // device. Initialised from current state (already applied at
        // connect) so the first iteration doesn't double-send.
        let mut last_sidetone = self.state.lock().unwrap().sidetone_level;

        while self.running.load(Ordering::Relaxed) {
            // Apply any pending sidetone change from the GUI. Cheap
            // — one mutex acquire per loop iteration, same pattern
            // we use for chatmix updates.
            let want_sidetone = self.state.lock().unwrap().sidetone_level;
            if want_sidetone != last_sidetone {
                if let Err(e) = dev.set_sidetone(want_sidetone) {
                    warn!("Failed to apply sidetone {want_sidetone}: {e}");
                } else {
                    last_sidetone = want_sidetone;
                }
            }

            // Short timeout keeps dial-to-update latency low; battery
            // polling still triggers every BATTERY_POLL_INTERVAL.
            match dev.read(100) {
                Ok(Some(msg)) => match NovaDevice::parse_event(&msg) {
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
                        {
                            let mut st = self.state.lock().unwrap();
                            st.battery = Some(bat.clone());
                        }
                        self.broadcast(DaemonEvent::Battery {
                            level: bat.level,
                            status: bat.status,
                        });
                    }
                    HidEvent::Unknown => {}
                },
                Ok(None) => {
                    if last_battery_poll.elapsed() >= BATTERY_POLL_INTERVAL {
                        self.poll_and_broadcast_battery(dev);
                        last_battery_poll = Instant::now();
                    }
                    if last_mic_health.elapsed() >= MIC_HEALTH_INTERVAL {
                        self.sinks.lock().unwrap().check_mic_health();
                        last_mic_health = Instant::now();
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

    /// Poll the device's battery state and fan it out to GUI subscribers.
    fn poll_and_broadcast_battery(&self, dev: &NovaDevice) {
        if let Ok(Some(bat)) = dev.get_battery() {
            {
                let mut st = self.state.lock().unwrap();
                st.battery = Some(bat.clone());
            }
            self.broadcast(DaemonEvent::Battery {
                level: bat.level,
                status: bat.status,
            });
        }
    }

    /// Wait for a duration, checking `running` every second.
    fn wait(&self, duration: Duration) {
        let start = Instant::now();
        while self.running.load(Ordering::Relaxed) && start.elapsed() < duration {
            thread::sleep(Duration::from_secs(1));
        }
    }
}
