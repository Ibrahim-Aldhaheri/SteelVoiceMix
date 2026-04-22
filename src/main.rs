mod audio;
mod display;
mod hid;
mod mixer;
mod protocol;

use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;

use log::{error, info, warn};

use audio::SinkManager;
use mixer::{broadcast_event, Mixer, MixerState, SharedSinks};
use protocol::{ClientCommand, DaemonEvent};

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
    }
}

fn handle_client(
    stream: UnixStream,
    state: Arc<Mutex<MixerState>>,
    subscribers: Arc<Mutex<Vec<std::sync::mpsc::Sender<DaemonEvent>>>>,
    sinks: SharedSinks,
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
                info!("GUI requested: remove media sink → enabled={enabled}");
                broadcast_event(&subscribers, DaemonEvent::MediaSinkChanged { enabled });
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
    let mut debug = false;
    for arg in std::env::args().skip(1) {
        match arg.as_str() {
            "--no-notify" => no_notify = true,
            "--no-socket" => no_socket = true,
            "--no-media-sink" => no_media_sink = true,
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
                println!("  --no-media-sink  Skip the NovaMedia sink on startup");
                println!("                   (the GUI can still add it at runtime)");
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

    let media_sink_enabled = !no_media_sink;
    let running = Arc::new(AtomicBool::new(true));
    let state = Arc::new(Mutex::new(MixerState::new(media_sink_enabled)));
    let subscribers: Arc<Mutex<Vec<std::sync::mpsc::Sender<DaemonEvent>>>> =
        Arc::new(Mutex::new(Vec::new()));
    let sinks: SharedSinks = Arc::new(Mutex::new(SinkManager::new(media_sink_enabled)));

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
                            let running = running.clone();
                            thread::spawn(move || {
                                handle_client(stream, state, subs, sinks, running);
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
