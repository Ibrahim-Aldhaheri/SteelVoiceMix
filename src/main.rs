mod audio;
mod config;
mod display;
mod filter_chain;
mod hid;
mod hotplug;
mod lockext;
mod mic_chain;
mod mixer;
mod protocol;
mod routing;
mod surround_chain;

use std::io::{BufRead, BufReader, Read, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;

use log::{error, info, warn};

use crate::lockext::LockExt;
use audio::SinkManager;
use filter_chain::{FilterChainHandle, FilterChainSpec};
use mixer::{broadcast_event, Mixer, MixerState, SharedSinks};
use protocol::{
    default_channel_bands, AncMode, ClientCommand, DaemonEvent, EqChannel, EqState, MicFeature,
    MicGain, MicState, PmShutdown, VolumeBoost, WirelessMode,
};
use routing::{spawn_router, RouterState};

/// Index of a surround channel key in the chain's fixed 7.1 order
/// (FL FR FC LFE RL RR SL SR) — the same order as `audio.position` on
/// `effect_input.SteelSurround` and as the test WAV's channel mask.
fn surround_channel_index(key: &str) -> Option<usize> {
    surround_chain::SURROUND_CHANNELS
        .iter()
        .position(|&k| k == key)
}

/// Number of channels in the surround chain — and in the test WAV.
const SURROUND_WAV_CHANNELS: u16 = 8;

/// `dwChannelMask` for FL FR FC LFE RL(BL) RR(BR) SL SR, in that order.
/// WAVE_FORMAT_EXTENSIBLE orders samples by ascending mask bit, so this
/// mask makes the file's channel order identical to the chain's
/// `audio.position` — no guessing about PipeWire's default layout for
/// an 8-channel file.
const SURROUND_WAV_MASK: u32 = 0x1 | 0x2 | 0x4 | 0x8 | 0x10 | 0x20 | 0x200 | 0x400;

/// Write the per-channel test burst: an 8-channel 48 kHz WAV that is
/// silent everywhere except `channel_index`.
///
/// Why 8 channels rather than the mono file this used to write: a mono
/// stream fed to the 8-channel surround sink goes through PipeWire's
/// channelmix, which *spreads* it — measured on target, mono +
/// `--channel-map=FL` produced only a 1.4:1 left/right ratio at the
/// binaural output, while a true FL-only 8-channel file produced 1.9:1.
/// The tone was audible but didn't localise, which reads as "the test
/// button does nothing".
///
/// The signal is a broadband burst (log-spaced partials, 300 Hz – 8 kHz)
/// rather than a 440 Hz sine: HRTF localisation cues live in spectral
/// shaping and high-frequency ITD/ILD, so a low pure tone is close to
/// the worst possible probe for "which speaker is this?".
///
/// Pure byte-writing (no hardware), so this part is deterministic and
/// unit-testable; only the playback routing below needs a real graph.
fn write_test_burst_wav(
    path: &std::path::Path,
    channel_index: usize,
) -> std::io::Result<()> {
    use std::io::Write;
    const RATE: u32 = 48_000;
    const SECS: f32 = 0.9;
    const PARTIALS: usize = 24;
    const F_LO: f32 = 300.0;
    const F_HI: f32 = 8_000.0;

    let ch = SURROUND_WAV_CHANNELS as u32;
    let n = (RATE as f32 * SECS) as u32;
    let block_align = ch * 2; // 16-bit
    let data_len = n * block_align;
    // fmt chunk is the 40-byte EXTENSIBLE form (16 base + cbSize + 22).
    let fmt_len: u32 = 40;
    let riff_len = 4 + (8 + fmt_len) + (8 + data_len);

    let mut buf: Vec<u8> = Vec::with_capacity((8 + riff_len) as usize);
    buf.extend_from_slice(b"RIFF");
    buf.extend_from_slice(&riff_len.to_le_bytes());
    buf.extend_from_slice(b"WAVE");
    buf.extend_from_slice(b"fmt ");
    buf.extend_from_slice(&fmt_len.to_le_bytes());
    buf.extend_from_slice(&0xFFFEu16.to_le_bytes()); // WAVE_FORMAT_EXTENSIBLE
    buf.extend_from_slice(&SURROUND_WAV_CHANNELS.to_le_bytes());
    buf.extend_from_slice(&RATE.to_le_bytes());
    buf.extend_from_slice(&(RATE * block_align).to_le_bytes()); // byte rate
    buf.extend_from_slice(&(block_align as u16).to_le_bytes());
    buf.extend_from_slice(&16u16.to_le_bytes()); // bits per sample
    buf.extend_from_slice(&22u16.to_le_bytes()); // cbSize
    buf.extend_from_slice(&16u16.to_le_bytes()); // valid bits
    buf.extend_from_slice(&SURROUND_WAV_MASK.to_le_bytes());
    // KSDATAFORMAT_SUBTYPE_PCM
    buf.extend_from_slice(&[
        0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x10, 0x00, 0x80, 0x00, 0x00, 0xAA, 0x00,
        0x38, 0x9B, 0x71,
    ]);
    buf.extend_from_slice(b"data");
    buf.extend_from_slice(&data_len.to_le_bytes());

    // Pre-compute the partials. Phases are spread deterministically (no
    // RNG — the same file every time is easier to reason about) so the
    // stacked sines don't all peak together.
    let mut freqs = [0.0f32; PARTIALS];
    let mut phases = [0.0f32; PARTIALS];
    for k in 0..PARTIALS {
        let frac = k as f32 / (PARTIALS - 1) as f32;
        freqs[k] = F_LO * (F_HI / F_LO).powf(frac);
        phases[k] = (k * k) as f32 * 0.618 * std::f32::consts::PI;
    }

    // Render, then normalise to a known peak. Scaling by 1/sqrt(N) is an
    // RMS-style guess and does clip on this phase set; measuring the
    // actual peak makes the output exactly -6 dBFS with no clipped
    // samples, which is both louder and cleaner than the old sine.
    let mut mono: Vec<f32> = Vec::with_capacity(n as usize);
    let mut peak = 0.0f32;
    for i in 0..n {
        let t = i as f32 / RATE as f32;
        // 15 ms fades so the onset isn't a click.
        let env = (t / 0.015).min(1.0).min((SECS - t) / 0.015).max(0.0);
        let mut s = 0.0f32;
        for k in 0..PARTIALS {
            s += (2.0 * std::f32::consts::PI * freqs[k] * t + phases[k]).sin();
        }
        let s = s * env;
        peak = peak.max(s.abs());
        mono.push(s);
    }
    let norm = if peak > 0.0 { 0.5 / peak } else { 0.0 };

    for s in &mono {
        let v = ((s * norm).clamp(-1.0, 1.0) * i16::MAX as f32) as i16;
        for c in 0..ch as usize {
            let sample = if c == channel_index { v } else { 0 };
            buf.extend_from_slice(&sample.to_le_bytes());
        }
    }
    std::fs::File::create(path)?.write_all(&buf)
}

/// Per-channel test burst. Writes an 8-channel WAV carrying signal only
/// on the requested channel and plays it into the surround sink, so the
/// channel routing is decided by the file's own layout rather than by
/// PipeWire's mono upmix (see `write_test_burst_wav`). No
/// `--channel-map` — the stream is already 8-channel and positioned.
fn play_surround_test_tone(channel: &str) {
    let Some(index) = surround_channel_index(channel) else {
        warn!("test-tone: unknown channel {channel}");
        return;
    };
    let dir = match std::env::var_os("XDG_RUNTIME_DIR") {
        Some(d) => PathBuf::from(d).join("steelvoicemix"),
        None => {
            warn!("test-tone: no XDG_RUNTIME_DIR");
            return;
        }
    };
    let _ = std::fs::create_dir_all(&dir);
    let wav = dir.join("test-tone.wav");
    if let Err(e) = write_test_burst_wav(&wav, index) {
        warn!("test-tone: could not write {}: {e}", wav.display());
        return;
    }
    let spawn = std::process::Command::new("pw-play")
        .arg("--target=effect_input.SteelSurround")
        .arg(&wav)
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::piped())
        .spawn();
    match spawn {
        // Reap in a detached thread — dropping the Child without waiting
        // leaves a zombie until the daemon exits, and users can press
        // Test many times. Report a non-zero exit / stderr: a silent
        // failure here is indistinguishable from "the button does
        // nothing", which is exactly how the last bug presented.
        Ok(child) => {
            let ch = channel.to_string();
            std::thread::spawn(move || match child.wait_with_output() {
                Ok(out) if out.status.success() => {}
                Ok(out) => warn!(
                    "test-tone {ch}: pw-play exited {} — {}",
                    out.status,
                    String::from_utf8_lossy(&out.stderr).trim()
                ),
                Err(e) => warn!("test-tone {ch}: waiting on pw-play failed: {e}"),
            });
        }
        Err(e) => warn!("test-tone: pw-play failed to start: {e}"),
    }
}

fn socket_path() -> PathBuf {
    // Prefer $XDG_RUNTIME_DIR (/run/user/<uid>, mode 0700, per-user).
    // Fall back to /run/user/<uid> directly rather than a bare file in
    // the world-writable /tmp: a predictable /tmp/steelvoicemix-<uid>
    // .sock can be squatted or connected to by another local user.
    // The socket is additionally chmod'd 0600 after bind (see below),
    // but the private runtime dir is the primary protection. Keep this
    // in sync with the GUI (gui/settings.py) and CLI fallbacks.
    let uid = unsafe { libc::getuid() };
    if let Ok(dir) = std::env::var("XDG_RUNTIME_DIR") {
        PathBuf::from(dir).join("steelvoicemix.sock")
    } else {
        PathBuf::from(format!("/run/user/{uid}")).join("steelvoicemix.sock")
    }
}

/// Snapshot current MixerState into a Status event. Used both for the
/// one-shot `status` query and the initial push on `subscribe`.
fn snapshot_status(state: &Arc<Mutex<MixerState>>) -> DaemonEvent {
    let st = state.lock_or_recover();
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
        volume_boost: st.volume_boost,
        oled_brightness: st.oled_brightness,
        oled_present: st.oled_present,
        anc_mode: st.anc_mode,
        anc_transparent_level: st.anc_transparent_level,
        wireless_mode: st.wireless_mode,
        mic_gain: st.mic_gain,
        mic_volume: st.mic_volume,
        mic_led_brightness: st.mic_led_brightness,
        pm_shutdown: st.pm_shutdown,
        deck_control_enabled: st.deck_control_enabled,
        device_variant: st.device_variant,
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
        let mut st = state.lock_or_recover();
        update(&mut st.mic_state);
        st.mic_state
    };
    let default_active = {
        let mut sm = sinks.lock_or_recover();
        sm.set_mic_state(new_state)
    };
    persist_sink_state(state);
    info!(
        "GUI requested: set-mic-{label} (enabled={enabled}, strength={strength})"
    );
    broadcast_event(subscribers, DaemonEvent::MicStateChanged { state: new_state });
    // The default-source change is a separate event so the GUI can
    // tie its one-time notification to it specifically (rather than
    // showing one every mic-state-changed event).
    broadcast_event(
        subscribers,
        DaemonEvent::MicDefaultSourceChanged {
            active: default_active,
        },
    );
}

/// Persist sink-toggle preferences. Reads all flags from the current
/// MixerState so a change to one doesn't clobber the others.
fn persist_sink_state(state: &Arc<Mutex<MixerState>>) {
    let st = state.lock_or_recover();
    config::save(&config::DaemonState {
        media_sink_enabled: st.media_sink_enabled,
        hdmi_sink_enabled: st.hdmi_sink_enabled,
        auto_route_browsers: st.auto_route_browsers,
        eq_enabled: st.eq_enabled,
        eq_state: st.eq_state,
        surround_enabled: st.surround_enabled,
        surround_hrir_path: st.surround_hrir_path.clone(),
        mic_state: st.mic_state,
        // Migration is a one-shot at process startup; once any
        // state has been persisted the marker should stay True
        // forever so subsequent restarts skip the override.
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
        pm_shutdown: st.pm_shutdown,
        deck_control_enabled: st.deck_control_enabled,
    });
}

/// Longest command line we'll accept from a client. `BufRead::lines`
/// grows its buffer until it finds a newline, so without a ceiling a
/// peer that streams bytes and never sends `\n` walks the daemon into
/// the OOM killer. Real commands are a few hundred bytes; 64 KiB is
/// enormous headroom (a full 10-band EQ payload is well under 4 KiB).
const MAX_COMMAND_LEN: u64 = 64 * 1024;

/// Cap on simultaneously-connected clients. One thread is spawned per
/// connection, so an unbounded accept loop is a cheap way to exhaust
/// thread/memory limits. The GUI uses two (one subscribe, one
/// request/response); the CLI adds one more per invocation.
const MAX_CLIENTS: usize = 16;

/// Verify the connecting process runs as the same uid as the daemon.
/// The socket already lives in a 0700 runtime dir and is chmod 0600,
/// so this is defence in depth — it closes the window where the socket
/// is somehow reachable (a misconfigured runtime dir, a future change
/// to socket_path) and makes the trust boundary explicit in code
/// rather than implicit in filesystem permissions.
///
/// Uses `SO_PEERCRED` via libc rather than `UnixStream::peer_cred()`,
/// which is still unstable in std as of the toolchains we build on.
fn peer_is_same_uid(stream: &UnixStream) -> bool {
    use std::os::unix::io::AsRawFd;

    let mut cred = libc::ucred {
        pid: 0,
        uid: u32::MAX,
        gid: u32::MAX,
    };
    let mut len = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
    // SAFETY: `cred` and `len` are correctly sized and aligned for
    // SO_PEERCRED on Linux, and `stream` owns a valid socket fd for
    // the duration of the call.
    let rc = unsafe {
        libc::getsockopt(
            stream.as_raw_fd(),
            libc::SOL_SOCKET,
            libc::SO_PEERCRED,
            std::ptr::addr_of_mut!(cred).cast::<libc::c_void>(),
            &mut len,
        )
    };
    if rc != 0 {
        // If the kernel won't tell us who's calling, fail closed.
        warn!(
            "Cannot read peer credentials, refusing client: {}",
            std::io::Error::last_os_error()
        );
        return false;
    }
    let ours = unsafe { libc::getuid() };
    if cred.uid != ours {
        warn!(
            "Rejecting socket client from uid {} (daemon runs as {})",
            cred.uid, ours
        );
        return false;
    }
    true
}

fn handle_client(
    stream: UnixStream,
    state: Arc<Mutex<MixerState>>,
    subscribers: Arc<Mutex<Vec<std::sync::mpsc::Sender<DaemonEvent>>>>,
    sinks: SharedSinks,
    router: Arc<RouterState>,
    running: Arc<AtomicBool>,
) {
    if !peer_is_same_uid(&stream) {
        return;
    }

    let peer_stream = match stream.try_clone() {
        Ok(s) => s,
        Err(_) => return,
    };

    // .take() bounds the whole conversation's *read* side per line:
    // we re-arm the limit each iteration so a long session of normal
    // commands is fine, but any single line over the cap ends the
    // connection instead of growing the buffer without limit.
    let mut reader = BufReader::new(stream);

    loop {
        if !running.load(Ordering::Relaxed) {
            break;
        }
        let mut line = String::new();
        let n = match (&mut reader)
            .take(MAX_COMMAND_LEN)
            .read_line(&mut line)
        {
            Ok(0) => break, // clean EOF
            Ok(n) => n,
            Err(_) => break,
        };
        // Hit the ceiling without a newline → oversized/hostile line.
        if n as u64 == MAX_COMMAND_LEN && !line.ends_with('\n') {
            warn!(
                "Client sent a line over {MAX_COMMAND_LEN} bytes; \
                 dropping connection"
            );
            break;
        }
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
                    let mut sm = sinks.lock_or_recover();
                    sm.enable_media()
                };
                {
                    let mut st = state.lock_or_recover();
                    st.media_sink_enabled = enabled;
                }
                persist_sink_state(&state);
                info!("GUI requested: add media sink → enabled={enabled}");
                broadcast_event(&subscribers, DaemonEvent::MediaSinkChanged { enabled });
            }
            ClientCommand::RemoveMediaSink => {
                let enabled = {
                    let mut sm = sinks.lock_or_recover();
                    sm.disable_media()
                };
                {
                    let mut st = state.lock_or_recover();
                    st.media_sink_enabled = enabled;
                }
                persist_sink_state(&state);
                info!("GUI requested: remove media sink → enabled={enabled}");
                broadcast_event(&subscribers, DaemonEvent::MediaSinkChanged { enabled });
            }
            ClientCommand::AddHdmiSink => {
                let enabled = {
                    let mut sm = sinks.lock_or_recover();
                    sm.enable_hdmi()
                };
                {
                    let mut st = state.lock_or_recover();
                    st.hdmi_sink_enabled = enabled;
                }
                persist_sink_state(&state);
                info!("GUI requested: add hdmi sink → enabled={enabled}");
                broadcast_event(&subscribers, DaemonEvent::HdmiSinkChanged { enabled });
            }
            ClientCommand::RemoveHdmiSink => {
                let enabled = {
                    let mut sm = sinks.lock_or_recover();
                    sm.disable_hdmi()
                };
                {
                    let mut st = state.lock_or_recover();
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
                    let mut st = state.lock_or_recover();
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
                    let mut sm = sinks.lock_or_recover();
                    if enabled {
                        sm.enable_eq()
                    } else {
                        sm.disable_eq()
                    }
                };
                {
                    let mut st = state.lock_or_recover();
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
                    let mut sm = sinks.lock_or_recover();
                    sm.set_eq_band_gain(channel, band, gain_db)
                };
                if result.is_none() {
                    warn!(
                        "Invalid set-eq-band-gain (channel={:?}, band={band}, gain_db={gain_db})",
                        channel
                    );
                    continue;
                }
                let new_state_all = sinks.lock_or_recover().eq_state();
                {
                    let mut st = state.lock_or_recover();
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
                    let mut sm = sinks.lock_or_recover();
                    sm.set_eq_channel_bands(channel, bands)
                };
                if result.is_none() {
                    warn!(
                        "Invalid set-eq-channel (channel={:?}, malformed bands)",
                        channel
                    );
                    continue;
                }
                let new_state_all = sinks.lock_or_recover().eq_state();
                {
                    let mut st = state.lock_or_recover();
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
                    let mut sm = sinks.lock_or_recover();
                    sm.reset_to_defaults();
                }
                {
                    let mut st = state.lock_or_recover();
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
                    st.oled_brightness = 8;
                    st.anc_mode = AncMode::Off;
                    st.anc_transparent_level = 5;
                    st.wireless_mode = WirelessMode::Speed;
                    st.mic_gain = MicGain::High;
                    st.mic_volume = 10;
                    st.mic_led_brightness = 10;
                    st.pm_shutdown = PmShutdown::ThirtyMinutes;
                    st.deck_control_enabled = false;
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
                    EqChannel::Mic,
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
                    DaemonEvent::MicDefaultSourceChanged { active: false },
                );
                broadcast_event(
                    &subscribers,
                    DaemonEvent::SidetoneChanged { level: 0 },
                );
                broadcast_event(
                    &subscribers,
                    DaemonEvent::NotificationsEnabledChanged { enabled: true },
                );
                broadcast_event(
                    &subscribers,
                    DaemonEvent::OledBrightnessChanged { level: 8 },
                );
                broadcast_event(
                    &subscribers,
                    DaemonEvent::AncModeChanged { mode: AncMode::Off },
                );
                broadcast_event(
                    &subscribers,
                    DaemonEvent::AncTransparentLevelChanged { level: 5 },
                );
                broadcast_event(
                    &subscribers,
                    DaemonEvent::WirelessModeChanged {
                        mode: WirelessMode::Speed,
                    },
                );
                broadcast_event(
                    &subscribers,
                    DaemonEvent::MicGainChanged { gain: MicGain::High },
                );
                broadcast_event(
                    &subscribers,
                    DaemonEvent::MicVolumeChanged { level: 10 },
                );
                broadcast_event(
                    &subscribers,
                    DaemonEvent::MicLedBrightnessChanged { level: 10 },
                );
                broadcast_event(
                    &subscribers,
                    DaemonEvent::PmShutdownChanged {
                        value: PmShutdown::ThirtyMinutes,
                    },
                );
                broadcast_event(
                    &subscribers,
                    DaemonEvent::DeckControlEnabledChanged { enabled: false },
                );
            }
            ClientCommand::SetSurroundEnabled { enabled } => {
                let actual = {
                    let mut sm = sinks.lock_or_recover();
                    sm.set_surround_enabled(enabled)
                };
                {
                    let mut st = state.lock_or_recover();
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
                    let mut sm = sinks.lock_or_recover();
                    sm.set_surround_hrir(new_path.clone())
                };
                {
                    let mut st = state.lock_or_recover();
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
            ClientCommand::SetSurroundChannelGain { channel, gain_db } => {
                let ok = {
                    let mut sm = sinks.lock_or_recover();
                    sm.set_surround_channel_gain(&channel, gain_db)
                };
                info!(
                    "GUI requested: set-surround-channel-gain {channel} = {gain_db} dB (ok={ok})"
                );
            }
            ClientCommand::SetSurroundGains { channels } => {
                let entries: Vec<(String, f32, bool)> = channels
                    .into_iter()
                    .map(|(k, v)| (k, v.gain_db, v.muted))
                    .collect();
                {
                    let mut sm = sinks.lock_or_recover();
                    sm.set_surround_gains_batch(&entries);
                }
                info!(
                    "GUI requested: set-surround-gains ({} channels)",
                    entries.len()
                );
            }
            ClientCommand::SetSurroundChannelMute { channel, muted } => {
                let ok = {
                    let mut sm = sinks.lock_or_recover();
                    sm.set_surround_channel_mute(&channel, muted)
                };
                info!(
                    "GUI requested: set-surround-channel-mute {channel} = {muted} (ok={ok})"
                );
            }
            ClientCommand::SetSurroundSolo { channel } => {
                {
                    let mut sm = sinks.lock_or_recover();
                    sm.set_surround_solo(&channel);
                }
                info!("GUI requested: set-surround-solo {channel:?}");
            }
            ClientCommand::SurroundTestTone { channel } => {
                // Only play when the surround chain is actually up —
                // otherwise `--target=effect_input.SteelSurround` names a
                // node that doesn't exist and pw-play would fall back to
                // the default sink, beeping out the wrong device.
                let running = {
                    let sm = sinks.lock_or_recover();
                    sm.surround_chain_running()
                };
                if running {
                    play_surround_test_tone(&channel);
                    info!("GUI requested: surround-test-tone {channel}");
                } else {
                    info!(
                        "Ignoring surround-test-tone {channel}: chain not running"
                    );
                }
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
            ClientCommand::SetMicVolumeStabilizer { enabled, strength, kind } => {
                handle_mic_feature_update(
                    &sinks,
                    &state,
                    &subscribers,
                    |s| {
                        s.volume_stabilizer = MicFeature { enabled, strength };
                        if let Some(k) = kind {
                            s.volume_stabilizer_kind = k;
                        }
                    },
                    "volume-stabilizer",
                    enabled,
                    strength,
                );
            }
            ClientCommand::SetChannelBoost {
                channel,
                enabled,
                multiplier_pct,
            } => {
                if matches!(channel, EqChannel::Mic) {
                    warn!("set-channel-boost rejected: mic channel has no sink-volume control");
                    continue;
                }
                let pct = multiplier_pct.clamp(100, 200);
                let new_boost = VolumeBoost {
                    enabled,
                    multiplier_pct: pct,
                };
                // Snapshot management — store the dial-at-the-moment
                // when boost transitions OFF→ON, restore it when boost
                // transitions ON→OFF. Slider drags while the toggle is
                // already on don't touch the snapshot. The restore is
                // baked into st.game_vol / st.chat_vol so the
                // immediate-apply call below sees the snapshot, and
                // any subsequent broadcast carries the restored value.
                let (
                    current_game,
                    current_chat,
                    restored_chatmix,
                ) = {
                    let mut st = state.lock_or_recover();
                    let was_enabled = st.volume_boost.for_channel(channel).enabled;
                    if let Some(slot) = st.volume_boost.for_channel_mut(channel) {
                        *slot = new_boost;
                    }
                    let mut restored = false;
                    match (was_enabled, enabled, channel) {
                        (false, true, EqChannel::Game) => {
                            st.pre_boost_game_vol = Some(st.game_vol);
                        }
                        (false, true, EqChannel::Chat) => {
                            st.pre_boost_chat_vol = Some(st.chat_vol);
                        }
                        (true, false, EqChannel::Game) => {
                            if let Some(snap) = st.pre_boost_game_vol.take() {
                                st.game_vol = snap;
                                restored = true;
                            }
                        }
                        (true, false, EqChannel::Chat) => {
                            if let Some(snap) = st.pre_boost_chat_vol.take() {
                                st.chat_vol = snap;
                                restored = true;
                            }
                        }
                        // Slider change while already on (or already
                        // off, or Media/HDMI which have no snapshot)
                        // — leave snapshot state untouched.
                        _ => {}
                    }
                    (st.game_vol, st.chat_vol, restored)
                };
                persist_sink_state(&state);
                // Apply right away — without this, Game/Chat would only
                // pick up the new multiplier on the next dial event, and
                // Media/HDMI would never apply at all (their volumes are
                // set once at sink-load time).
                match channel {
                    EqChannel::Game => {
                        SinkManager::set_volume(audio::GAME_SINK, new_boost.apply(current_game));
                    }
                    EqChannel::Chat => {
                        SinkManager::set_volume(audio::CHAT_SINK, new_boost.apply(current_chat));
                    }
                    EqChannel::Media => {
                        // Media sink has no chatmix-derived volume — base is 100%.
                        SinkManager::set_volume(audio::MEDIA_SINK, new_boost.apply(100));
                    }
                    EqChannel::Hdmi => {
                        SinkManager::set_volume(audio::HDMI_SINK, new_boost.apply(100));
                    }
                    EqChannel::Mic => unreachable!(),
                }
                info!(
                    "GUI requested: set-channel-boost → channel={:?} enabled={} pct={} restored={}",
                    channel, enabled, pct, restored_chatmix
                );
                broadcast_event(
                    &subscribers,
                    DaemonEvent::ChannelBoostChanged {
                        channel,
                        boost: new_boost,
                    },
                );
                // Replay a fresh chatmix event when we restored a
                // snapshot so the GUI overlay (and any other
                // chatmix-following client) reflects the new
                // effective balance instead of the now-stale dial
                // reading the user adjusted during the boost.
                if restored_chatmix {
                    broadcast_event(
                        &subscribers,
                        DaemonEvent::ChatMix {
                            game: current_game,
                            chat: current_chat,
                        },
                    );
                }
            }
            ClientCommand::SetSidetone { level } => {
                let clamped = level.min(128);
                {
                    let mut st = state.lock_or_recover();
                    st.sidetone_level = clamped;
                }
                persist_sink_state(&state);
                info!("GUI requested: set-sidetone level={clamped} — applied on next event-loop iteration");
                broadcast_event(
                    &subscribers,
                    DaemonEvent::SidetoneChanged { level: clamped },
                );
            }
            ClientCommand::SetOledBrightness { level } => {
                let clamped = level.clamp(1, 10);
                let oled_present = {
                    let mut st = state.lock_or_recover();
                    st.oled_brightness = clamped;
                    st.oled_present
                };
                persist_sink_state(&state);
                let suffix = if oled_present {
                    "applied on next iteration"
                } else {
                    "stored; no OLED on connected device"
                };
                info!("GUI requested: set-oled-brightness level={clamped} ({suffix})");
                broadcast_event(
                    &subscribers,
                    DaemonEvent::OledBrightnessChanged { level: clamped },
                );
            }
            ClientCommand::SetAncMode { mode } => {
                {
                    let mut st = state.lock_or_recover();
                    st.anc_mode = mode;
                }
                persist_sink_state(&state);
                info!("GUI requested: set-anc-mode mode={mode:?}");
                broadcast_event(
                    &subscribers,
                    DaemonEvent::AncModeChanged { mode },
                );
            }
            ClientCommand::SetAncTransparentLevel { level } => {
                let clamped = level.clamp(1, 10);
                {
                    let mut st = state.lock_or_recover();
                    st.anc_transparent_level = clamped;
                }
                persist_sink_state(&state);
                info!("GUI requested: set-anc-transparent-level level={clamped}");
                broadcast_event(
                    &subscribers,
                    DaemonEvent::AncTransparentLevelChanged { level: clamped },
                );
            }
            ClientCommand::SetWirelessMode { mode } => {
                let already = {
                    let mut st = state.lock_or_recover();
                    let already = st.wireless_mode == mode;
                    if !already {
                        st.wireless_mode = mode;
                    }
                    already
                };
                if already {
                    info!(
                        "GUI requested: set-wireless-mode {mode:?} — no-op (already in this mode; skipping HID write to avoid bouncing the radio)"
                    );
                    // Re-broadcast so any out-of-sync GUI client snaps
                    // back to the correct state. Idempotent.
                    broadcast_event(
                        &subscribers,
                        DaemonEvent::WirelessModeChanged { mode },
                    );
                } else {
                    persist_sink_state(&state);
                    info!("GUI requested: set-wireless-mode {mode:?} — radio will briefly drop link");
                    broadcast_event(
                        &subscribers,
                        DaemonEvent::WirelessModeChanged { mode },
                    );
                }
            }
            ClientCommand::ToggleWirelessMode => {
                let new_mode = {
                    let mut st = state.lock_or_recover();
                    let flipped = st.wireless_mode.flipped();
                    st.wireless_mode = flipped;
                    flipped
                };
                persist_sink_state(&state);
                info!("CLI/GUI requested: toggle-wireless-mode → {new_mode:?}");
                broadcast_event(
                    &subscribers,
                    DaemonEvent::WirelessModeChanged { mode: new_mode },
                );
            }
            ClientCommand::SetMicGain { gain } => {
                {
                    let mut st = state.lock_or_recover();
                    st.mic_gain = gain;
                }
                persist_sink_state(&state);
                info!("GUI requested: set-mic-gain {gain:?}");
                broadcast_event(&subscribers, DaemonEvent::MicGainChanged { gain });
            }
            ClientCommand::SetMicVolume { level } => {
                let clamped = level.clamp(1, 10);
                {
                    let mut st = state.lock_or_recover();
                    st.mic_volume = clamped;
                }
                persist_sink_state(&state);
                info!("GUI requested: set-mic-volume level={clamped}");
                broadcast_event(
                    &subscribers,
                    DaemonEvent::MicVolumeChanged { level: clamped },
                );
            }
            ClientCommand::SetMicLedBrightness { level } => {
                let clamped = level.clamp(1, 10);
                {
                    let mut st = state.lock_or_recover();
                    st.mic_led_brightness = clamped;
                }
                persist_sink_state(&state);
                info!("GUI requested: set-mic-led-brightness level={clamped}");
                broadcast_event(
                    &subscribers,
                    DaemonEvent::MicLedBrightnessChanged { level: clamped },
                );
            }
            ClientCommand::SetPmShutdown { value } => {
                {
                    let mut st = state.lock_or_recover();
                    st.pm_shutdown = value;
                }
                persist_sink_state(&state);
                info!("GUI requested: set-pm-shutdown {value:?}");
                broadcast_event(
                    &subscribers,
                    DaemonEvent::PmShutdownChanged { value },
                );
            }
            ClientCommand::SetDeckControlEnabled { enabled } => {
                let was = {
                    let mut st = state.lock_or_recover();
                    let was = st.deck_control_enabled;
                    st.deck_control_enabled = enabled;
                    // Flipping false→true: reset all `applied_*`
                    // sentinels so the event loop sees mismatches and
                    // pushes every persisted setting on its next
                    // iteration. The OLED brightness can't be re-pushed
                    // on the existing passive handle though — that
                    // requires a session restart. We log a hint.
                    if enabled && !was {
                        st.applied_oled_brightness = 0;
                        st.applied_anc_mode = u8::MAX;
                        st.applied_anc_transparent_level = 0;
                        st.applied_wireless_mode = u8::MAX;
                        st.applied_mic_gain = u8::MAX;
                        st.applied_mic_volume = 0;
                        st.applied_mic_led_brightness = 0;
                        st.applied_pm_shutdown = u8::MAX;
                    }
                    was
                };
                persist_sink_state(&state);
                info!(
                    "GUI requested: set-deck-control-enabled {enabled} (was {was}){}",
                    if enabled && !was {
                        " — re-applying persisted settings on next iteration. OLED brightness will take effect on next reconnect."
                    } else {
                        ""
                    }
                );
                broadcast_event(
                    &subscribers,
                    DaemonEvent::DeckControlEnabledChanged { enabled },
                );
            }
            ClientCommand::SetNotificationsEnabled { enabled } => {
                {
                    let mut st = state.lock_or_recover();
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
                    let mut sm = sinks.lock_or_recover();
                    sm.set_eq_band(channel, band, params)
                };
                if result.is_none() {
                    warn!(
                        "Invalid set-eq-band (channel={:?}, band={band}, params={:?})",
                        channel, params
                    );
                    continue;
                }
                let new_state_all = sinks.lock_or_recover().eq_state();
                {
                    let mut st = state.lock_or_recover();
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
                subscribers.lock_or_recover().push(tx);

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
    let mut persisted = config::load();
    // One-shot migration: existing configs from before mic-defaults
    // shipped have all-zero MicState. Apply the new sensible default
    // (gate on at strength 60) and flip the marker so we don't
    // override user choices on subsequent starts.
    if !persisted.mic_default_applied {
        info!(
            "Applying mic-state default (one-shot migration): {:?}",
            MicState::default()
        );
        persisted.mic_state = MicState::default();
        persisted.mic_default_applied = true;
        // Write back immediately so the marker flips on disk before
        // any future state change overwrites the file. Otherwise a
        // crash between migration and the first persist_sink_state()
        // would re-trigger the migration on next start.
        config::save(&persisted);
    }
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
    let volume_boost = persisted.volume_boost;
    let persisted_game_vol = persisted.game_vol;
    let persisted_chat_vol = persisted.chat_vol;
    let oled_brightness = persisted.oled_brightness.clamp(1, 10);
    let anc_mode = persisted.anc_mode;
    let anc_transparent_level = persisted.anc_transparent_level.clamp(1, 10);
    let wireless_mode = persisted.wireless_mode;
    let mic_gain = persisted.mic_gain;
    let mic_volume = persisted.mic_volume.clamp(1, 10);
    let mic_led_brightness = persisted.mic_led_brightness.clamp(1, 10);
    let pm_shutdown = persisted.pm_shutdown;
    let deck_control_enabled = persisted.deck_control_enabled;
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
        "Sidetone level: {} | Daemon notifications: {} | OLED brightness: {} | ANC: {:?} (transparent level {}) | Wireless mode: {:?} | Mic gain: {:?} | Mic vol: {} | Mic LED: {} | PM shutdown: {:?} | Deck control: {}",
        sidetone_level, notifications_enabled, oled_brightness,
        anc_mode, anc_transparent_level, wireless_mode,
        mic_gain, mic_volume, mic_led_brightness, pm_shutdown, deck_control_enabled,
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
        volume_boost,
        persisted_game_vol,
        persisted_chat_vol,
        oled_brightness,
        anc_mode,
        anc_transparent_level,
        wireless_mode,
        mic_gain,
        mic_volume,
        mic_led_brightness,
        pm_shutdown,
        deck_control_enabled,
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

    // USB hotplug watcher — gives us instant disconnect/reconnect
    // signals via libusb's hotplug API, instead of relying entirely
    // on the slower battery-poll watchdog. The flag starts at false
    // (we don't yet know) and gets set once libusb's `enumerate`
    // pass fires `device_arrived` for an already-connected device.
    let device_present = Arc::new(AtomicBool::new(false));
    let _hotplug_watcher = hotplug::start(device_present.clone());

    // Start mixer thread
    let mut mixer = Mixer::new(
        running.clone(),
        state.clone(),
        subscribers.clone(),
        sinks.clone(),
        !no_notify,
        device_present.clone(),
    );
    let mixer_thread = thread::spawn(move || mixer.run());

    // Socket server
    if !no_socket {
        let sock_path = socket_path();
        // Remove stale socket
        let _ = std::fs::remove_file(&sock_path);

        match UnixListener::bind(&sock_path) {
            Ok(listener) => {
                // Restrict the socket to the owner (0600). Defence in
                // depth on top of the private runtime dir: even if the
                // socket ever lands somewhere world-traversable, no
                // other local user can connect() without write access.
                if let Err(e) = std::fs::set_permissions(
                    &sock_path,
                    std::os::unix::fs::PermissionsExt::from_mode(0o600),
                ) {
                    warn!("Could not chmod 0600 {}: {e}", sock_path.display());
                }
                info!("Socket server listening on {}", sock_path.display());
                // Set non-blocking so we can check `running`
                listener
                    .set_nonblocking(true)
                    .expect("Failed to set non-blocking");

                // Live client count, decremented when a handler thread
                // exits. Bounds how many threads a peer can force us to
                // spawn by opening connections in a loop.
                let clients = Arc::new(std::sync::atomic::AtomicUsize::new(0));

                while running.load(Ordering::Relaxed) {
                    match listener.accept() {
                        Ok((stream, _)) => {
                            if clients.load(Ordering::Relaxed) >= MAX_CLIENTS {
                                warn!(
                                    "Refusing connection: {MAX_CLIENTS} clients \
                                     already connected"
                                );
                                // Dropping the stream closes it; the
                                // client sees EOF and can retry.
                                continue;
                            }
                            let state = state.clone();
                            let subs = subscribers.clone();
                            let sinks = sinks.clone();
                            let router = router.clone();
                            let running = running.clone();
                            let clients = clients.clone();
                            clients.fetch_add(1, Ordering::Relaxed);
                            thread::spawn(move || {
                                handle_client(stream, state, subs, sinks, router, running);
                                clients.fetch_sub(1, Ordering::Relaxed);
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

#[cfg(test)]
mod test_tone_tests {
    use super::*;

    fn read_u32(b: &[u8], at: usize) -> u32 {
        u32::from_le_bytes([b[at], b[at + 1], b[at + 2], b[at + 3]])
    }
    fn read_u16(b: &[u8], at: usize) -> u16 {
        u16::from_le_bytes([b[at], b[at + 1]])
    }

    #[test]
    fn channel_index_matches_chain_order() {
        // The WAV's channel order must line up with the chain's
        // audio.position, or "test FL" plays out of the wrong speaker.
        assert_eq!(surround_channel_index("fl"), Some(0));
        assert_eq!(surround_channel_index("fr"), Some(1));
        assert_eq!(surround_channel_index("fc"), Some(2));
        assert_eq!(surround_channel_index("lfe"), Some(3));
        assert_eq!(surround_channel_index("rl"), Some(4));
        assert_eq!(surround_channel_index("rr"), Some(5));
        assert_eq!(surround_channel_index("sl"), Some(6));
        assert_eq!(surround_channel_index("sr"), Some(7));
        assert_eq!(surround_channel_index("nope"), None);
    }

    #[test]
    fn burst_wav_header_is_8ch_extensible() {
        let dir = std::env::temp_dir().join("svm-burst-header");
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("t.wav");
        write_test_burst_wav(&path, 0).unwrap();
        let b = std::fs::read(&path).unwrap();

        assert_eq!(&b[0..4], b"RIFF");
        assert_eq!(&b[8..12], b"WAVE");
        assert_eq!(&b[12..16], b"fmt ");
        assert_eq!(read_u32(&b, 16), 40, "EXTENSIBLE fmt chunk");
        assert_eq!(read_u16(&b, 20), 0xFFFE, "WAVE_FORMAT_EXTENSIBLE");
        assert_eq!(read_u16(&b, 22), 8, "8 channels");
        assert_eq!(read_u32(&b, 24), 48_000);
        assert_eq!(read_u16(&b, 32), 16, "block align = 8ch * 2 bytes");
        assert_eq!(read_u16(&b, 34), 16, "16-bit");
        assert_eq!(
            read_u32(&b, 40),
            SURROUND_WAV_MASK,
            "channel mask must spell FL FR FC LFE RL RR SL SR"
        );
        // RIFF size and data size must agree with the actual bytes.
        assert_eq!(read_u32(&b, 4) as usize, b.len() - 8);
        let data_len = read_u32(&b, 64) as usize;
        assert_eq!(&b[60..64], b"data");
        assert_eq!(data_len, b.len() - 68);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn burst_wav_carries_signal_on_exactly_one_channel() {
        let dir = std::env::temp_dir().join("svm-burst-isolation");
        std::fs::create_dir_all(&dir).unwrap();
        for target in 0..8usize {
            let path = dir.join(format!("t{target}.wav"));
            write_test_burst_wav(&path, target).unwrap();
            let b = std::fs::read(&path).unwrap();
            let data = &b[68..];
            let frames = data.len() / 16;
            let mut peak = [0i32; 8];
            for f in 0..frames {
                for (c, p) in peak.iter_mut().enumerate() {
                    let at = f * 16 + c * 2;
                    let v = i16::from_le_bytes([data[at], data[at + 1]]) as i32;
                    *p = (*p).max(v.abs());
                }
            }
            for (c, p) in peak.iter().enumerate() {
                if c == target {
                    // Loud enough to actually notice — the old 440 Hz
                    // sine peaked at -12 dBFS and read as "nothing
                    // happened" on hardware.
                    assert!(
                        *p > 8_000,
                        "channel {c} should carry the burst; peak {p}"
                    );
                } else {
                    assert_eq!(*p, 0, "channel {c} must be silent");
                }
            }
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn burst_wav_does_not_clip() {
        // 24 stacked partials must stay inside full scale, otherwise the
        // "broadband" burst is just distortion.
        let dir = std::env::temp_dir().join("svm-burst-clip");
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("t.wav");
        write_test_burst_wav(&path, 3).unwrap();
        let b = std::fs::read(&path).unwrap();
        let data = &b[68..];
        let frames = data.len() / 16;
        let mut clipped = 0;
        for f in 0..frames {
            let at = f * 16 + 3 * 2;
            let v = i16::from_le_bytes([data[at], data[at + 1]]);
            if v == i16::MAX || v == i16::MIN {
                clipped += 1;
            }
        }
        assert_eq!(clipped, 0, "{clipped} clipped samples");
        let _ = std::fs::remove_dir_all(&dir);
    }
}
