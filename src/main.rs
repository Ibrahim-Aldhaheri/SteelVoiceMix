mod audio;
mod config;
mod display;
mod filter_chain;
mod hid;
mod mic_chain;
mod mixer;
mod protocol;
mod routing;
mod surround_chain;

use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;

use log::{error, info, warn};

use audio::SinkManager;
use filter_chain::{FilterChainHandle, FilterChainSpec};
use mixer::{broadcast_event, Mixer, MixerState, SharedSinks};
use protocol::{
    default_channel_bands, ClientCommand, DaemonEvent, EqChannel, EqState, MicFeature,
    MicState,
};
use routing::{spawn_router, RouterState};

fn socket_path() -> PathBuf {
    // Use XDG_RUNTIME_DIR if available, fallback to /tmp
    if let Ok(dir) = std::env::var("XDG_RUNTIME_DIR") {
        PathBuf::from(dir).join("steelvoicemix.sock")
    } else {
        PathBuf::from("/tmp")
            .join(format!("steelvoicemix-{}.sock", unsafe { libc::getuid() }))
    }
}

/// Snapshot current MixerState into a Status event. Used both for the
/// one-shot `status` query and the initial push on `subscribe`.
fn snapshot_status(state: &Arc<Mutex<MixerState>>) -> DaemonEvent {
    let st = state.lock().unwrap();
    DaemonEvent::Status {
        connected: st.connected,
        game_vol: st.game_vol,
        chat_vol: st.chat_vol,
        battery: st.battery.clone(),
        media_sink_enabled: st.media_sink_enabled,
        hdmi_sink_enabled: st.hdmi_sink_enabled,
        auto_route_browsers: st.auto_route_browsers,
        eq_enabled: st.eq_enabled,
        eq_state: Box::new(st.eq_state),
        surround_enabled: st.surround_enabled,
        surround_hrir_path: st
            .surround_hrir_path
            .as_ref()
            .map(|p| p.display().to_string()),
        mic_state: st.mic_state,
        sidetone_level: st.sidetone_level,
        notifications_enabled: st.notifications_enabled,
    }
}

/// Apply a single-feature update to the persisted MicState, push it
/// into the SinkManager (which respawns the chain), persist, and
/// broadcast the new state. Used by the three mic-feature command
/// handlers — they only differ in which field of MicState they
/// mutate, captured by the `update` closure.
fn handle_mic_feature_update(
    sinks: &SharedSinks,
    state: &Arc<Mutex<MixerState>>,
    subscribers: &Arc<Mutex<Vec<std::sync::mpsc::Sender<DaemonEvent>>>>,
    update: impl FnOnce(&mut MicState),
    label: &str,
    enabled: bool,
    strength: u8,
) {
    let new_state: MicState = {
        let mut st = state.lock().unwrap();
        update(&mut st.mic_state);
        st.mic_state
    };
    {
        let mut sm = sinks.lock().unwrap();
        sm.set_mic_state(new_state);
    }
    persist_sink_state(state);
    info!(
        "GUI requested: set-mic-{label} (enabled={enabled}, strength={strength})"
    );
    broadcast_event(subscribers, DaemonEvent::MicStateChanged { state: new_state });
}

/// Persist sink-toggle preferences. Reads all flags from the current
/// MixerState so a change to one doesn't clobber the others.
fn persist_sink_state(state: &Arc<Mutex<MixerState>>) {
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
        sidetone_level: st.sidetone_level,
        notifications_enabled: st.notifications_enabled,
    });
}

fn handle_client(
    stream: UnixStream,
    state: Arc<Mutex<MixerState>>,
    subscribers: Arc<Mutex<Vec<std::sync::mpsc::Sender<DaemonEvent>>>>,
    sinks: SharedSinks,
    router: Arc<RouterState>,
    running: Arc<AtomicBool>,
) {
    let peer_stream = match stream.try_clone() {
        Ok(s) => s,
        Err(_) => return,
    };

    let reader = BufReader::new(stream);

    for line in reader.lines() {
        if !running.load(Ordering::Relaxed) {
            break;
        }
        let line = match line {
            Ok(l) => l,
            Err(_) => break,
        };
        if line.trim().is_empty() {
            continue;
        }

        let cmd: ClientCommand = match serde_json::from_str(&line) {
            Ok(c) => c,
            Err(e) => {
                warn!("Invalid command from client: {e}");
                continue;
            }
        };

        match cmd {
            ClientCommand::Status => {
                let event = snapshot_status(&state);
                let mut json = serde_json::to_string(&event).unwrap();
                json.push('\n');
                let mut w = &peer_stream;
                if w.write_all(json.as_bytes()).is_err() {
                    break;
                }
            }
            ClientCommand::AddMediaSink => {
                let enabled = {
                    let mut sm = sinks.lock().unwrap();
                    sm.enable_media()
                };
                {
                    let mut st = state.lock().unwrap();
                    st.media_sink_enabled = enabled;
                }
                persist_sink_state(&state);
                info!("GUI requested: add media sink → enabled={enabled}");
                broadcast_event(&subscribers, DaemonEvent::MediaSinkChanged { enabled });
            }
            ClientCommand::RemoveMediaSink => {
                let enabled = {
                    let mut sm = sinks.lock().unwrap();
                    sm.disable_media()
                };
                {
                    let mut st = state.lock().unwrap();
                    st.media_sink_enabled = enabled;
                }
                persist_sink_state(&state);
                info!("GUI requested: remove media sink → enabled={enabled}");
                broadcast_event(&subscribers, DaemonEvent::MediaSinkChanged { enabled });
            }
            ClientCommand::AddHdmiSink => {
                let enabled = {
                    let mut sm = sinks.lock().unwrap();
                    sm.enable_hdmi()
                };
                {
                    let mut st = state.lock().unwrap();
                    st.hdmi_sink_enabled = enabled;
                }
                persist_sink_state(&state);
                info!("GUI requested: add hdmi sink → enabled={enabled}");
                broadcast_event(&subscribers, DaemonEvent::HdmiSinkChanged { enabled });
            }
            ClientCommand::RemoveHdmiSink => {
                let enabled = {
                    let mut sm = sinks.lock().unwrap();
                    sm.disable_hdmi()
                };
                {
                    let mut st = state.lock().unwrap();
                    st.hdmi_sink_enabled = enabled;
                }
                persist_sink_state(&state);
                info!("GUI requested: remove hdmi sink → enabled={enabled}");
                broadcast_event(&subscribers, DaemonEvent::HdmiSinkChanged { enabled });
            }
            ClientCommand::SetAutoRouteBrowsers { enabled } => {
                router
                    .enabled
                    .store(enabled, std::sync::atomic::Ordering::Relaxed);
                {
                    let mut st = state.lock().unwrap();
                    st.auto_route_browsers = enabled;
                }
                persist_sink_state(&state);
                info!("GUI requested: auto-route browsers → enabled={enabled}");
                broadcast_event(
                    &subscribers,
                    DaemonEvent::AutoRouteBrowsersChanged { enabled },
                );
            }
            ClientCommand::SetEqEnabled { enabled } => {
                let actual = {
                    let mut sm = sinks.lock().unwrap();
                    if enabled {
                        sm.enable_eq()
                    } else {
                        sm.disable_eq()
                    }
                };
                {
                    let mut st = state.lock().unwrap();
                    st.eq_enabled = actual;
                }
                persist_sink_state(&state);
                info!("GUI requested: set-eq-enabled → enabled={actual}");
                broadcast_event(
                    &subscribers,
                    DaemonEvent::EqEnabledChanged { enabled: actual },
                );
            }
            ClientCommand::SetEqBandGain {
                channel,
                band,
                gain_db,
            } => {
                let result = {
                    let mut sm = sinks.lock().unwrap();
                    sm.set_eq_band_gain(channel, band, gain_db)
                };
                if result.is_none() {
                    warn!(
                        "Invalid set-eq-band-gain (channel={:?}, band={band}, gain_db={gain_db})",
                        channel
                    );
                    continue;
                }
                let new_state_all = sinks.lock().unwrap().eq_state();
                {
                    let mut st = state.lock().unwrap();
                    st.eq_state = new_state_all;
                }
                persist_sink_state(&state);
                let channel_bands = new_state_all.for_channel(channel);
                info!(
                    "GUI requested: set-eq-band-gain → channel={:?} band={band} gain={:.2} dB",
                    channel,
                    channel_bands[(band - 1) as usize].gain
                );
                broadcast_event(
                    &subscribers,
                    DaemonEvent::EqBandsChanged {
                        channel,
                        bands: channel_bands,
                    },
                );
            }
            ClientCommand::SetEqChannel { channel, bands } => {
                let result = {
                    let mut sm = sinks.lock().unwrap();
                    sm.set_eq_channel_bands(channel, bands)
                };
                if result.is_none() {
                    warn!(
                        "Invalid set-eq-channel (channel={:?}, malformed bands)",
                        channel
                    );
                    continue;
                }
                let new_state_all = sinks.lock().unwrap().eq_state();
                {
                    let mut st = state.lock().unwrap();
                    st.eq_state = new_state_all;
                }
                persist_sink_state(&state);
                let channel_bands = new_state_all.for_channel(channel);
                info!(
                    "GUI requested: set-eq-channel → channel={:?} (preset load)",
                    channel
                );
                broadcast_event(
                    &subscribers,
                    DaemonEvent::EqBandsChanged {
                        channel,
                        bands: channel_bands,
                    },
                );
            }
            ClientCommand::ResetState => {
                {
                    let mut sm = sinks.lock().unwrap();
                    sm.reset_to_defaults();
                }
                {
                    let mut st = state.lock().unwrap();
                    st.media_sink_enabled = false;
                    st.hdmi_sink_enabled = false;
                    st.auto_route_browsers = false;
                    st.eq_enabled = false;
                    st.eq_state = EqState::default();
                    st.surround_enabled = false;
                    st.surround_hrir_path = None;
                    st.mic_state = MicState::default();
                    st.sidetone_level = 0;
                    st.notifications_enabled = true;
                }
                router
                    .enabled
                    .store(false, std::sync::atomic::Ordering::Relaxed);
                persist_sink_state(&state);
                info!("GUI requested: reset-state — every runtime pref reset to defaults");
                // Fire one event per affected facet so the GUI can
                // refresh each tab without a separate Status round
                // trip.
                broadcast_event(
                    &subscribers,
                    DaemonEvent::MediaSinkChanged { enabled: false },
                );
                broadcast_event(
                    &subscribers,
                    DaemonEvent::HdmiSinkChanged { enabled: false },
                );
                broadcast_event(
                    &subscribers,
                    DaemonEvent::AutoRouteBrowsersChanged { enabled: false },
                );
                broadcast_event(
                    &subscribers,
                    DaemonEvent::EqEnabledChanged { enabled: false },
                );
                let flat = EqState::default();
                for ch in [
                    EqChannel::Game,
                    EqChannel::Chat,
                    EqChannel::Media,
                    EqChannel::Hdmi,
                ] {
                    broadcast_event(
                        &subscribers,
                        DaemonEvent::EqBandsChanged {
                            channel: ch,
                            bands: flat.for_channel(ch),
                        },
                    );
                }
                broadcast_event(
                    &subscribers,
                    DaemonEvent::SurroundEnabledChanged { enabled: false },
                );
                broadcast_event(
                    &subscribers,
                    DaemonEvent::SurroundHrirChanged { path: None },
                );
                broadcast_event(
                    &subscribers,
                    DaemonEvent::MicStateChanged {
                        state: MicState::default(),
                    },
                );
                broadcast_event(
                    &subscribers,
                    DaemonEvent::SidetoneChanged { level: 0 },
                );
                broadcast_event(
                    &subscribers,
                    DaemonEvent::NotificationsEnabledChanged { enabled: true },
                );
            }
            ClientCommand::SetSurroundEnabled { enabled } => {
                let actual = {
                    let mut sm = sinks.lock().unwrap();
                    sm.set_surround_enabled(enabled)
                };
                {
                    let mut st = state.lock().unwrap();
                    st.surround_enabled = actual;
                }
                persist_sink_state(&state);
                info!(
                    "GUI requested: set-surround-enabled requested={enabled}, applied={actual}"
                );
                broadcast_event(
                    &subscribers,
                    DaemonEvent::SurroundEnabledChanged { enabled: actual },
                );
            }
            ClientCommand::SetSurroundHrir { path } => {
                let new_path: Option<std::path::PathBuf> = path
                    .as_deref()
                    .map(|s| s.trim())
                    .filter(|s| !s.is_empty())
                    .map(std::path::PathBuf::from);
                let stored = {
                    let mut sm = sinks.lock().unwrap();
                    sm.set_surround_hrir(new_path.clone())
                };
                {
                    let mut st = state.lock().unwrap();
                    st.surround_hrir_path = stored.clone();
                    // set_surround_hrir disables surround if the path
                    // gets cleared while running — mirror that here.
                    if stored.is_none() {
                        st.surround_enabled = false;
                    }
                }
                persist_sink_state(&state);
                let display_path = stored.as_ref().map(|p| p.display().to_string());
                info!(
                    "GUI requested: set-surround-hrir → {}",
                    display_path.as_deref().unwrap_or("<cleared>")
                );
                broadcast_event(
                    &subscribers,
                    DaemonEvent::SurroundHrirChanged { path: display_path },
                );
            }
            ClientCommand::SetMicNoiseGate { enabled, strength } => {
                handle_mic_feature_update(
                    &sinks,
                    &state,
                    &subscribers,
                    |s| s.noise_gate = MicFeature { enabled, strength },
                    "noise-gate",
                    enabled,
                    strength,
                );
            }
            ClientCommand::SetMicNoiseReduction { enabled, strength } => {
                handle_mic_feature_update(
                    &sinks,
                    &state,
                    &subscribers,
                    |s| s.noise_reduction = MicFeature { enabled, strength },
                    "noise-reduction",
                    enabled,
                    strength,
                );
            }
            ClientCommand::SetMicAiNoiseCancellation { enabled, strength } => {
                handle_mic_feature_update(
                    &sinks,
                    &state,
                    &subscribers,
                    |s| s.ai_noise_cancellation = MicFeature { enabled, strength },
                    "ai-nc",
                    enabled,
                    strength,
                );
            }
            ClientCommand::SetSidetone { level } => {
                let clamped = level.min(128);
                {
                    let mut st = state.lock().unwrap();
                    st.sidetone_level = clamped;
                }
                persist_sink_state(&state);
                info!("GUI requested: set-sidetone level={clamped} — applied on next event-loop iteration");
                broadcast_event(
                    &subscribers,
                    DaemonEvent::SidetoneChanged { level: clamped },
                );
            }
            ClientCommand::SetNotificationsEnabled { enabled } => {
                {
                    let mut st = state.lock().unwrap();
                    st.notifications_enabled = enabled;
                }
                persist_sink_state(&state);
                info!("GUI requested: set-notifications-enabled={enabled}");
                broadcast_event(
                    &subscribers,
                    DaemonEvent::NotificationsEnabledChanged { enabled },
                );
            }
            ClientCommand::SetEqBand {
                channel,
                band,
                params,
            } => {
                let result = {
                    let mut sm = sinks.lock().unwrap();
                    sm.set_eq_band(channel, band, params)
                };
                if result.is_none() {
                    warn!(
                        "Invalid set-eq-band (channel={:?}, band={band}, params={:?})",
                        channel, params
                    );
                    continue;
                }
                let new_state_all = sinks.lock().unwrap().eq_state();
                {
                    let mut st = state.lock().unwrap();
                    st.eq_state = new_state_all;
                }
                persist_sink_state(&state);
                let channel_bands = new_state_all.for_channel(channel);
                info!(
                    "GUI requested: set-eq-band → channel={:?} band={band} params={:?}",
                    channel, channel_bands[(band - 1) as usize]
                );
                broadcast_event(
                    &subscribers,
                    DaemonEvent::EqBandsChanged {
                        channel,
                        bands: channel_bands,
                    },
                );
            }
            ClientCommand::Subscribe => {
                let (tx, rx) = std::sync::mpsc::channel::<DaemonEvent>();
                subscribers.lock().unwrap().push(tx);

                // Send current status immediately
                {
                    let event = snapshot_status(&state);
                    let mut json = serde_json::to_string(&event).unwrap();
                    json.push('\n');
                    let mut w = &peer_stream;
                    if w.write_all(json.as_bytes()).is_err() {
                        return;
                    }
                }

                // Stream events until client disconnects or daemon stops
                let mut w = &peer_stream;
                while running.load(Ordering::Relaxed) {
                    match rx.recv_timeout(std::time::Duration::from_secs(1)) {
                        Ok(event) => {
                            let mut json = serde_json::to_string(&event).unwrap();
                            json.push('\n');
                            if w.write_all(json.as_bytes()).is_err() {
                                return;
                            }
                        }
                        Err(std::sync::mpsc::RecvTimeoutError::Timeout) => continue,
                        Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => return,
                    }
                }
                return;
            }
        }
    }
}

fn main() {
    // Parse args
    let mut no_notify = false;
    let mut no_socket = false;
    let mut no_media_sink = false;
    let mut no_hdmi_sink = false;
    let mut debug = false;
    let mut test_filter_chain = false;
    for arg in std::env::args().skip(1) {
        match arg.as_str() {
            "--no-notify" => no_notify = true,
            "--no-socket" => no_socket = true,
            "--no-media-sink" => no_media_sink = true,
            "--no-hdmi-sink" => no_hdmi_sink = true,
            "--test-filter-chain" => test_filter_chain = true,
            "--debug" | "-d" => debug = true,
            "--version" | "-V" => {
                println!("steelvoicemix {}", env!("CARGO_PKG_VERSION"));
                return;
            }
            "--help" | "-h" => {
                println!("steelvoicemix — ChatMix daemon for SteelSeries Arctis Nova Pro Wireless");
                println!();
                println!("Usage: steelvoicemix [OPTIONS]");
                println!();
                println!("Options:");
                println!("  --no-notify      Disable desktop notifications");
                println!("  --no-socket      Disable Unix socket server (no GUI support)");
                println!("  --no-media-sink  Skip the SteelMedia sink on startup");
                println!("                   (the GUI can still add it at runtime)");
                println!("  --no-hdmi-sink   Skip the SteelHDMI sink on startup");
                println!("                   (the GUI can still add it at runtime)");
                println!("  --test-filter-chain  Load a passthrough filter-chain sink");
                println!("                   ('SteelGameEQ_test') for 5 seconds, then");
                println!("                   unload and exit. Used to verify PipeWire");
                println!("                   filter-chain wiring before EQ ships.");
                println!("  -d, --debug      Enable debug logging (equivalent to RUST_LOG=debug)");
                println!("  -V, --version    Print version and exit");
                println!("  -h, --help       Show this help");
                return;
            }
            other => {
                eprintln!("Unknown option: {other}");
                std::process::exit(1);
            }
        }
    }

    // Init logging — default info, overridable by RUST_LOG, forced debug by --debug
    let default_level = if debug { "debug" } else { "info" };
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or(default_level))
        .format_timestamp_secs()
        .init();

    if test_filter_chain {
        run_filter_chain_test();
        return;
    }

    // Optional sinks (Media, HDMI): default to whatever the user picked last
    // time. On a fresh install the state file doesn't exist and load() returns
    // the Default (both false) — new users aren't surprised by extra output
    // devices they didn't ask for. --no-* CLI flags are session-only overrides
    // that do NOT overwrite the stored preference.
    let persisted = config::load();
    let media_sink_enabled = if no_media_sink { false } else { persisted.media_sink_enabled };
    let hdmi_sink_enabled = if no_hdmi_sink { false } else { persisted.hdmi_sink_enabled };
    let auto_route_browsers = persisted.auto_route_browsers;
    let eq_enabled = persisted.eq_enabled;
    let eq_state = persisted.eq_state;
    let surround_enabled = persisted.surround_enabled;
    let surround_hrir_path = persisted.surround_hrir_path.clone();
    let mic_state = persisted.mic_state;
    let sidetone_level = persisted.sidetone_level;
    let notifications_enabled = persisted.notifications_enabled;
    info!(
        "Media sink startup state: {} (persisted={}, --no-media-sink={})",
        media_sink_enabled, persisted.media_sink_enabled, no_media_sink
    );
    info!(
        "HDMI sink startup state: {} (persisted={}, --no-hdmi-sink={})",
        hdmi_sink_enabled, persisted.hdmi_sink_enabled, no_hdmi_sink
    );
    info!("Browser auto-routing: {}", auto_route_browsers);
    info!(
        "EQ filter chains: {} ({} bands per channel)",
        eq_enabled,
        protocol::NUM_BANDS,
    );
    info!(
        "Surround: {} (HRIR: {})",
        surround_enabled,
        surround_hrir_path
            .as_ref()
            .map(|p| p.display().to_string())
            .unwrap_or_else(|| "<not set>".into()),
    );
    info!(
        "Mic processing: gate={} nr={} ai_nc={}",
        mic_state.noise_gate.enabled,
        mic_state.noise_reduction.enabled,
        mic_state.ai_noise_cancellation.enabled,
    );
    info!(
        "Sidetone level: {} | Daemon notifications: {}",
        sidetone_level, notifications_enabled,
    );
    let running = Arc::new(AtomicBool::new(true));
    let state = Arc::new(Mutex::new(MixerState::new(
        media_sink_enabled,
        hdmi_sink_enabled,
        auto_route_browsers,
        eq_enabled,
        eq_state,
        surround_enabled,
        surround_hrir_path.clone(),
        mic_state,
        sidetone_level,
        notifications_enabled,
    )));
    let subscribers: Arc<Mutex<Vec<std::sync::mpsc::Sender<DaemonEvent>>>> =
        Arc::new(Mutex::new(Vec::new()));
    let sinks: SharedSinks = Arc::new(Mutex::new(SinkManager::new(
        media_sink_enabled,
        hdmi_sink_enabled,
        eq_enabled,
        eq_state,
        surround_enabled,
        surround_hrir_path,
        mic_state,
    )));
    let router = Arc::new(RouterState::new(auto_route_browsers));
    spawn_router(router.clone(), running.clone());

    // Signal handling
    {
        let running = running.clone();
        ctrlc::set_handler(move || {
            info!("Shutting down...");
            running.store(false, Ordering::Relaxed);
        })
        .expect("Failed to set signal handler");
    }

    // Start mixer thread
    let mut mixer = Mixer::new(
        running.clone(),
        state.clone(),
        subscribers.clone(),
        sinks.clone(),
        !no_notify,
    );
    let mixer_thread = thread::spawn(move || mixer.run());

    // Socket server
    if !no_socket {
        let sock_path = socket_path();
        // Remove stale socket
        let _ = std::fs::remove_file(&sock_path);

        match UnixListener::bind(&sock_path) {
            Ok(listener) => {
                info!("Socket server listening on {}", sock_path.display());
                // Set non-blocking so we can check `running`
                listener
                    .set_nonblocking(true)
                    .expect("Failed to set non-blocking");

                while running.load(Ordering::Relaxed) {
                    match listener.accept() {
                        Ok((stream, _)) => {
                            let state = state.clone();
                            let subs = subscribers.clone();
                            let sinks = sinks.clone();
                            let router = router.clone();
                            let running = running.clone();
                            thread::spawn(move || {
                                handle_client(stream, state, subs, sinks, router, running);
                            });
                        }
                        Err(ref e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                            thread::sleep(std::time::Duration::from_millis(200));
                        }
                        Err(e) => {
                            error!("Socket accept error: {e}");
                            thread::sleep(std::time::Duration::from_secs(1));
                        }
                    }
                }

                // Cleanup socket
                let _ = std::fs::remove_file(&sock_path);
            }
            Err(e) => {
                error!("Failed to bind socket {}: {e}", sock_path.display());
                error!("Running without socket server (GUI won't work)");
                // Wait for mixer thread
                let _ = mixer_thread.join();
                return;
            }
        }
    }

    let _ = mixer_thread.join();
}

/// Diagnostic helper for `--test-filter-chain`. Spawns a passthrough
/// filter-chain sink as a managed child pipewire process, sleeps a few
/// seconds so the user can verify it shows up in `pactl list short sinks`,
/// then shuts it down cleanly and exits.
///
/// This is the "scaffolding works" smoke test before the full EQ
/// integration touches `audio.rs` and the loopback wiring.
fn run_filter_chain_test() {
    info!("=== filter-chain wiring smoke test ===");

    // Auto-detect the headset so the test chain has somewhere to play to.
    // Falls back to a sentinel if no headset is currently attached — the
    // chain still loads, just with no audio downstream.
    let headset = SinkManager::find_output_sink()
        .unwrap_or_else(|| "@DEFAULT_SINK@".to_string());
    info!("Using playback target: {headset}");

    let spec = FilterChainSpec {
        sink_name: "SteelGameEQ_test",
        description: "SteelVoiceMix filter-chain test",
        playback_target: &headset,
        bands: default_channel_bands(),
    };
    let Some(handle) = FilterChainHandle::spawn(&spec) else {
        error!("Failed to spawn test filter chain — see warnings above.");
        std::process::exit(1);
    };

    // Give pipewire a moment to actually load the module before we tell
    // the user to peek at pactl. The child is spawned asynchronously and
    // module loading inside it takes ~50–200 ms.
    std::thread::sleep(std::time::Duration::from_millis(500));
    info!(
        "Filter chain spawned. Verify: \
         pactl list short sinks | grep SteelGameEQ_test"
    );
    info!("Sleeping 5s before shutdown...");
    std::thread::sleep(std::time::Duration::from_secs(5));

    handle.shutdown();
    info!("Test filter chain shut down. Exiting.");
}
