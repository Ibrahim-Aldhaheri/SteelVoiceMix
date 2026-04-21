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
use crate::protocol::DaemonEvent;

const RECONNECT_BASE: Duration = Duration::from_secs(3);
const RECONNECT_MAX: Duration = Duration::from_secs(30);
const BATTERY_POLL_INTERVAL: Duration = Duration::from_secs(60);

/// Shared mixer state accessible by the socket server.
pub struct MixerState {
    pub connected: bool,
    pub game_vol: u8,
    pub chat_vol: u8,
    pub battery: Option<BatteryStatus>,
}

impl MixerState {
    pub fn new() -> Self {
        MixerState {
            connected: false,
            game_vol: 100,
            chat_vol: 100,
            battery: None,
        }
    }
}

/// The core mixer. Runs in its own thread, broadcasting events to subscribers.
pub struct Mixer {
    running: Arc<AtomicBool>,
    state: Arc<Mutex<MixerState>>,
    subscribers: Arc<Mutex<Vec<std::sync::mpsc::Sender<DaemonEvent>>>>,
    notify_enabled: bool,
}

impl Mixer {
    pub fn new(
        running: Arc<AtomicBool>,
        state: Arc<Mutex<MixerState>>,
        subscribers: Arc<Mutex<Vec<std::sync::mpsc::Sender<DaemonEvent>>>>,
        notify_enabled: bool,
    ) -> Self {
        Mixer {
            running,
            state,
            subscribers,
            notify_enabled,
        }
    }

    /// Broadcast an event to all subscribed GUI clients. Removes dead senders.
    fn broadcast(&self, event: DaemonEvent) {
        let mut subs = self.subscribers.lock().unwrap();
        subs.retain(|tx| tx.send(event.clone()).is_ok());
    }

    /// Send a desktop notification via notify-send.
    fn notify(&self, summary: &str, body: &str) {
        if !self.notify_enabled {
            return;
        }
        let mut cmd = std::process::Command::new("notify-send");
        cmd.args(["-a", "nova-mixer", "-i", "audio-headset", summary]);
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
            "nova-mixer v{} starting (notify={}, RUST_LOG={})",
            env!("CARGO_PKG_VERSION"),
            self.notify_enabled,
            std::env::var("RUST_LOG").unwrap_or_else(|_| "info".into()),
        );
        let mut reconnect_wait = RECONNECT_BASE;

        while self.running.load(Ordering::Relaxed) {
            // Find output sink
            let output_sink = match SinkManager::find_output_sink() {
                Some(s) => {
                    info!("Detected headset output sink: {s}");
                    s
                }
                None => {
                    warn!("Output sink not found — is the headset connected?");
                    self.wait(reconnect_wait);
                    reconnect_wait = (reconnect_wait * 2).min(RECONNECT_MAX);
                    continue;
                }
            };

            // Open HID device
            let dev = match NovaDevice::open() {
                Ok(d) => d,
                Err(e) => {
                    warn!("{e} — waiting...");
                    self.wait(reconnect_wait);
                    reconnect_wait = (reconnect_wait * 2).min(RECONNECT_MAX);
                    continue;
                }
            };

            // Reset backoff on successful connection
            reconnect_wait = RECONNECT_BASE;

            // Enable ChatMix
            if let Err(e) = dev.enable_chatmix() {
                warn!("Lost connection during setup: {e}");
                continue;
            }
            info!("Base station connected, ChatMix enabled");

            // Attach to the OLED display now that the device is present.
            // Scoped to this connection — dropped on disconnect, re-opened on reconnect.
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

            // Create sinks
            let mut sinks = SinkManager::new();
            sinks.create_sinks(&output_sink);

            // Ask the device for the current dial position. If the firmware
            // doesn't answer, fall back to the last-known state so a brief
            // disconnect doesn't clobber whatever the user had dialed in.
            let (init_game, init_chat) = match dev.get_chatmix() {
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
            };

            // Apply to the new sinks so they match what the GUI/OLED will show.
            SinkManager::set_volume(GAME_SINK, init_game);
            SinkManager::set_volume(CHAT_SINK, init_chat);

            // Update state
            {
                let mut st = self.state.lock().unwrap();
                st.connected = true;
                st.game_vol = init_game;
                st.chat_vol = init_chat;
            }

            self.broadcast(DaemonEvent::Connected);
            // Re-broadcast the current dial so GUIs that were watching a
            // Disconnected event update their bars immediately.
            self.broadcast(DaemonEvent::ChatMix {
                game: init_game,
                chat: init_chat,
            });
            if let Some(ref mut d) = display {
                if !d.show(init_game, init_chat) {
                    warn!("OLED gauge failing — disabling for this session");
                    display = None;
                }
            }
            self.notify(
                "🎧 ChatMix Active",
                "NovaGame and NovaChat sinks ready.\nUse the dial to control balance.",
            );

            // Poll battery on connect
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

            // Event loop
            info!("Listening for ChatMix dial events...");
            let mut last_battery_poll = Instant::now();
            let mut disconnected = false;

            while self.running.load(Ordering::Relaxed) && !disconnected {
                // Short timeout keeps dial-to-update latency low; battery
                // polling still triggers every BATTERY_POLL_INTERVAL.
                match dev.read(100) {
                    Ok(Some(msg)) => match NovaDevice::parse_event(&msg) {
                        HidEvent::ChatMix { game_vol, chat_vol } => {
                            debug!("dial: game={game_vol}% chat={chat_vol}%");
                            SinkManager::set_volume(GAME_SINK, game_vol);
                            SinkManager::set_volume(CHAT_SINK, chat_vol);
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
                            if let Some(ref mut d) = display {
                                if !d.show(game_vol, chat_vol) {
                                    warn!(
                                        "OLED gauge failing — disabling for this session"
                                    );
                                    display = None;
                                }
                            }
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
                        // Timeout — poll battery periodically
                        if last_battery_poll.elapsed() >= BATTERY_POLL_INTERVAL {
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
                            last_battery_poll = Instant::now();
                        }
                    }
                    Err(_) => {
                        warn!("Device disconnected");
                        disconnected = true;
                    }
                }
            }

            // Cleanup
            if let Some(ref mut d) = display {
                d.clear();
            }
            drop(display);
            let _ = dev.disable_chatmix();
            sinks.destroy_sinks();
            {
                let mut st = self.state.lock().unwrap();
                st.connected = false;
            }
            self.broadcast(DaemonEvent::Disconnected);

            if disconnected {
                self.notify("🎧 Base Station Disconnected", "Waiting for reconnect...");
                if self.running.load(Ordering::Relaxed) {
                    info!("Will attempt reconnect...");
                }
            }
        }

        info!("nova-mixer stopped");
    }

    /// Wait for a duration, checking `running` every second.
    fn wait(&self, duration: Duration) {
        let start = Instant::now();
        while self.running.load(Ordering::Relaxed) && start.elapsed() < duration {
            thread::sleep(Duration::from_secs(1));
        }
    }
}
