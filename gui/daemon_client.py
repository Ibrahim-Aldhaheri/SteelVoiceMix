"""Unix-socket client for the SteelVoiceMix daemon.

Runs in its own thread, subscribes to the daemon's event stream, and
re-emits each event as a Qt signal on the main thread.
"""

from __future__ import annotations

import json
import socket
import time

from PySide6.QtCore import QObject, Signal

from .settings import socket_path


def _normalize_bands(raw: list) -> list[dict]:
    """Coerce daemon-sent band dicts into a uniform GUI-side shape.

    The daemon emits each band as `{freq, q, gain, type, enabled}` (matching
    the `parametricEQ.filterN` preset JSON layout). We always return a list
    of dicts with those exact keys and type-coerced numeric values, so
    downstream slots never have to defend against partial / legacy shapes.
    Older `eq_gains` snapshots — bare floats — get wrapped into peaking
    bands at 1 kHz so the GUI still has something coherent to show until
    the next live event refreshes the real frequencies.
    """
    out: list[dict] = []
    for entry in raw:
        if isinstance(entry, dict):
            out.append({
                "freq": float(entry.get("freq", 1000.0)),
                "q": float(entry.get("q", 1.0)),
                "gain": float(entry.get("gain", 0.0)),
                "type": str(entry.get("type", "peaking")),
                "enabled": bool(entry.get("enabled", True)),
            })
        else:
            # Backwards-compat for the old EqGains shape (bare gain floats).
            out.append({
                "freq": 1000.0,
                "q": 1.0,
                "gain": float(entry),
                "type": "peaking",
                "enabled": True,
            })
    return out


_MIC_FEATURE_KEYS = (
    "noise_gate",
    "noise_reduction",
    "ai_noise_cancellation",
    "volume_stabilizer",
)
# Valid VolumeStabilizerKind enum values from the daemon. Default
# to "broadcast" — matches the Rust Default impl.
_VOLUME_STABILIZER_KINDS = ("broadcast", "soft")


def _normalize_mic_state(raw: dict) -> dict:
    """Coerce the daemon's MicState JSON into a uniform GUI-side
    shape: every feature key always present with bool `enabled` and
    int `strength` (0..100). Anything missing falls back to off / 0
    so the receiver never has to defend against partial dicts."""
    out: dict[str, dict] = {}
    for key in _MIC_FEATURE_KEYS:
        feat = raw.get(key) if isinstance(raw, dict) else None
        if isinstance(feat, dict):
            out[key] = {
                "enabled": bool(feat.get("enabled", False)),
                "strength": int(feat.get("strength", 0)),
            }
        else:
            out[key] = {"enabled": False, "strength": 0}
    # Volume Stabilizer's kind is at the MicState top level (not
    # inside a per-feature dict). Pass through if valid; default to
    # broadcast otherwise.
    kind = (raw.get("volume_stabilizer_kind") if isinstance(raw, dict) else "")
    out["volume_stabilizer_kind"] = (
        kind if kind in _VOLUME_STABILIZER_KINDS else "broadcast"
    )
    return out


class DaemonSignals(QObject):
    connected = Signal()
    disconnected = Signal()
    chatmix_changed = Signal(int, int)
    status_message = Signal(str)
    battery_updated = Signal(int, str)
    media_sink_changed = Signal(bool)
    hdmi_sink_changed = Signal(bool)
    auto_route_browsers_changed = Signal(bool)
    eq_enabled_changed = Signal(bool)
    # Emits (channel, bands) where channel is "game" or "chat" and bands
    # is a Python list of NUM_BANDS dicts, each shaped like the EqBand
    # struct on the daemon side: {freq, q, gain, type, enabled}. The full
    # band parameters (not just gain) come along so a preset load can
    # update frequency / Q / type labels too.
    eq_bands_changed = Signal(str, list)
    # One-shot: full Status snapshot's per-channel band data, sent at
    # startup so the GUI can populate both Game and Chat sliders before
    # the user has interacted with anything. Shape: {"game": [bands],
    # "chat": [bands]}.
    eq_full_state = Signal(dict)
    # Surround toggle + HRIR path. Path arrives as a str (or "" for
    # cleared); the GUI normalises empty strings to "no path".
    surround_enabled_changed = Signal(bool)
    surround_hrir_changed = Signal(str)
    # Microphone capture-side processing. Emits the full MicState
    # snapshot every time so the GUI doesn't have to remember which
    # feature changed — it just re-applies the snapshot to its three
    # toggles + sliders. Shape:
    #   {"noise_gate": {"enabled": bool, "strength": int},
    #    "noise_reduction": {...}, "ai_noise_cancellation": {...}}
    mic_state_changed = Signal(dict)
    # Hardware sidetone level (0..=128 normalised on the wire).
    sidetone_changed = Signal(int)
    # Daemon-side desktop notification toggle (separate from the GUI's
    # own minimize-to-tray toast; this one gates the connect /
    # disconnect notify-send popups emitted by the Rust daemon).
    notifications_enabled_changed = Signal(bool)
    # Daemon promoted (or restored) SteelMic as the system default
    # source. `active=True` = SteelMic is now the default; the GUI
    # uses this for a one-time "we changed your default mic" notice.
    mic_default_source_changed = Signal(bool)
    # Per-channel volume boost — emitted when the daemon applies a
    # SetChannelBoost or replays state on (re)subscribe. Args:
    # (channel: str ("game"/"chat"/"media"/"hdmi"), enabled: bool,
    # multiplier_pct: int (100..=200)).
    channel_boost_changed = Signal(str, bool, int)
    # Snapshot replay of the full per-channel boost state, fired from
    # the Status event so the Sinks tab can populate every row before
    # the user touches anything. Shape: {"game": {"enabled": bool,
    # "multiplier_pct": int}, "chat": {...}, ...}.
    volume_boost_state = Signal(dict)


class DaemonClient:
    """Reader loop — retries forever until `stop()` is called."""

    def __init__(self, signals: DaemonSignals):
        self.signals = signals
        self.running = True
        self._sock: socket.socket | None = None

    def run(self) -> None:
        while self.running:
            try:
                self._connect_and_subscribe()
            except Exception:
                pass
            if self.running:
                from PySide6.QtCore import QCoreApplication
                self.signals.status_message.emit(
                    QCoreApplication.translate(
                        "DaemonClient", "🔍 Connecting to daemon..."
                    )
                )
                time.sleep(2)

    def _connect_and_subscribe(self) -> None:
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.settimeout(5)
        self._sock.connect(socket_path())
        self._sock.settimeout(None)
        self._sock.sendall(b'{"cmd":"subscribe"}\n')

        buf = b""
        while self.running:
            try:
                self._sock.settimeout(2)
                data = self._sock.recv(4096)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.strip():
                        self._handle_event(json.loads(line))
            except socket.timeout:
                # No data this tick — keep reading. Only a non-timeout error
                # means the socket is broken; fall back to the outer reconnect.
                continue
            except Exception:
                break
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass

    def _handle_event(self, event: dict) -> None:
        ev = event.get("event", "")
        if ev == "chatmix":
            self.signals.chatmix_changed.emit(
                event.get("game", 0), event.get("chat", 0)
            )
        elif ev == "battery":
            self.signals.battery_updated.emit(
                event.get("level", 0), event.get("status", "offline")
            )
        elif ev == "connected":
            self.signals.connected.emit()
        elif ev == "disconnected":
            self.signals.disconnected.emit()
        elif ev == "media-sink-changed":
            self.signals.media_sink_changed.emit(bool(event.get("enabled", False)))
        elif ev == "hdmi-sink-changed":
            self.signals.hdmi_sink_changed.emit(bool(event.get("enabled", False)))
        elif ev == "auto-route-browsers-changed":
            self.signals.auto_route_browsers_changed.emit(
                bool(event.get("enabled", False))
            )
        elif ev == "eq-enabled-changed":
            self.signals.eq_enabled_changed.emit(bool(event.get("enabled", False)))
        elif ev == "surround-enabled-changed":
            self.signals.surround_enabled_changed.emit(
                bool(event.get("enabled", False))
            )
        elif ev == "surround-hrir-changed":
            path = event.get("path") or ""
            self.signals.surround_hrir_changed.emit(str(path))
        elif ev == "mic-state-changed":
            mic = event.get("state")
            if isinstance(mic, dict):
                self.signals.mic_state_changed.emit(_normalize_mic_state(mic))
        elif ev == "sidetone-changed":
            self.signals.sidetone_changed.emit(int(event.get("level", 0)))
        elif ev == "notifications-enabled-changed":
            self.signals.notifications_enabled_changed.emit(
                bool(event.get("enabled", True))
            )
        elif ev == "mic-default-source-changed":
            self.signals.mic_default_source_changed.emit(
                bool(event.get("active", False))
            )
        elif ev == "channel-boost-changed":
            channel = event.get("channel", "")
            boost = event.get("boost") or {}
            if channel in ("game", "chat", "media", "hdmi") and isinstance(boost, dict):
                self.signals.channel_boost_changed.emit(
                    channel,
                    bool(boost.get("enabled", False)),
                    int(boost.get("multiplier_pct", 100)),
                )
        elif ev == "eq-bands-changed":
            channel = event.get("channel", "")
            bands = event.get("bands")
            if (
                channel in ("game", "chat", "media", "hdmi", "mic")
                and isinstance(bands, list)
                and bands
            ):
                self.signals.eq_bands_changed.emit(channel, _normalize_bands(bands))
        elif ev == "status":
            self.signals.media_sink_changed.emit(
                bool(event.get("media_sink_enabled", True))
            )
            self.signals.hdmi_sink_changed.emit(
                bool(event.get("hdmi_sink_enabled", False))
            )
            self.signals.auto_route_browsers_changed.emit(
                bool(event.get("auto_route_browsers", False))
            )
            self.signals.eq_enabled_changed.emit(
                bool(event.get("eq_enabled", False))
            )
            self.signals.surround_enabled_changed.emit(
                bool(event.get("surround_enabled", False))
            )
            hrir = event.get("surround_hrir_path") or ""
            self.signals.surround_hrir_changed.emit(str(hrir))
            mic = event.get("mic_state")
            if isinstance(mic, dict):
                self.signals.mic_state_changed.emit(_normalize_mic_state(mic))
            self.signals.sidetone_changed.emit(int(event.get("sidetone_level", 0)))
            self.signals.notifications_enabled_changed.emit(
                bool(event.get("notifications_enabled", True))
            )
            boost = event.get("volume_boost")
            if isinstance(boost, dict):
                normalized: dict[str, dict] = {}
                for ch in ("game", "chat", "media", "hdmi"):
                    raw = boost.get(ch)
                    if isinstance(raw, dict):
                        normalized[ch] = {
                            "enabled": bool(raw.get("enabled", False)),
                            "multiplier_pct": int(raw.get("multiplier_pct", 100)),
                        }
                if normalized:
                    self.signals.volume_boost_state.emit(normalized)
            eq_state = event.get("eq_state") or event.get("eq_gains")
            if isinstance(eq_state, dict):
                state: dict[str, list[dict]] = {}
                for ch in ("game", "chat", "media", "hdmi", "mic"):
                    raw = eq_state.get(ch)
                    if isinstance(raw, list) and raw:
                        state[ch] = _normalize_bands(raw)
                if state:
                    self.signals.eq_full_state.emit(state)
            if event.get("connected"):
                self.signals.connected.emit()
                self.signals.chatmix_changed.emit(
                    event.get("game_vol", 100), event.get("chat_vol", 100)
                )
                bat = event.get("battery")
                if isinstance(bat, dict):
                    self.signals.battery_updated.emit(
                        bat.get("level", 0), bat.get("status", "offline")
                    )
            else:
                self.signals.disconnected.emit()

    def send_command(self, cmd: str, **extra) -> None:
        """Send a one-off command on a fresh short-lived connection.

        Why not the subscribe socket: once the daemon receives `subscribe`
        on a connection, `handle_client()` enters the event-streaming loop
        and stops reading from that socket entirely. Reusing it would put
        commands into the kernel buffer where no one reads them.
        Fire-and-forget: if the daemon is down there's nothing to do.

        `extra` kwargs are merged into the JSON payload alongside `cmd` so
        commands that need parameters (e.g. set-auto-route-browsers needs
        `enabled`) work without a second method.
        """
        payload = {"cmd": cmd}
        payload.update(extra)
        body = (json.dumps(payload) + "\n").encode()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(2)
                sock.connect(socket_path())
                sock.sendall(body)
        except Exception:
            pass

    def stop(self) -> None:
        self.running = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
