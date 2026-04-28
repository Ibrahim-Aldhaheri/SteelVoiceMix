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
    # Emits the full 6-band gain array as a Python list of floats.
    eq_band_gains_changed = Signal(list)


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
                self.signals.status_message.emit("🔍 Connecting to daemon...")
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
        elif ev == "eq-band-gains-changed":
            gains = event.get("gains")
            if isinstance(gains, list) and len(gains) == 6:
                self.signals.eq_band_gains_changed.emit(
                    [float(g) for g in gains]
                )
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
            gains = event.get("eq_band_gains")
            if isinstance(gains, list) and len(gains) == 6:
                self.signals.eq_band_gains_changed.emit(
                    [float(g) for g in gains]
                )
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
