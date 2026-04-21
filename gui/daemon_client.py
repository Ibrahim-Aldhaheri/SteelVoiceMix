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
        elif ev == "status":
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

    def stop(self) -> None:
        self.running = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
