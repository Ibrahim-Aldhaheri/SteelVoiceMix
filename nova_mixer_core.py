#!/usr/bin/python3
"""nova-mixer — ChatMix for SteelSeries Arctis Nova Pro Wireless on Linux.

Creates two virtual PipeWire sinks (Game + Chat) and maps the hardware
ChatMix dial on the base station to control their volume balance.

Requires: python3-hidapi, PipeWire, pactl
"""

import sys
import signal
import subprocess
import time
import logging
from pathlib import Path

try:
    import hid
except ImportError:
    print("Error: python-hidapi not found. Install it:")
    print("  Fedora: sudo dnf install python3-hidapi")
    print("  Ubuntu: sudo apt install python3-hid")
    sys.exit(1)

LOG_FMT = "%(asctime)s [%(levelname)s] %(message)s"
logging.basicConfig(format=LOG_FMT, level=logging.INFO)
log = logging.getLogger("nova-mixer")

# ── Desktop Notifications ───────────────────────────────
NOTIFY_ENABLED = True
APP_ICON = "audio-headset"  # Standard KDE/freedesktop icon


def notify(summary: str, body: str = "", icon: str = APP_ICON):
    """Send a desktop notification via notify-send (works on KDE/GNOME/etc)."""
    if not NOTIFY_ENABLED:
        return
    try:
        cmd = ["notify-send", "-a", "nova-mixer", "-i", icon, summary]
        if body:
            cmd.append(body)
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        pass  # notify-send not installed, silently skip

# ── USB IDs ──────────────────────────────────────────────
VENDOR_ID = 0x1038   # SteelSeries
PRODUCT_ID = 0x12E0  # Arctis Nova Pro Wireless base station
HID_INTERFACE = 0x04  # Control interface
MSG_LEN = 64

# ── HID Protocol ────────────────────────────────────────
TX = 0x06  # Host → base station
RX = 0x07  # Base station → host

# Parameter IDs (second byte)
OPT_SONAR_ICON = 0x8D     # Toggle Sonar icon on base station
OPT_CHATMIX_ENABLE = 0x49 # Enable ChatMix dial mode
OPT_VOLUME = 0x25         # Volume attenuation (0=max, 56=mute)
OPT_CHATMIX = 0x45        # ChatMix data (game_vol, chat_vol)
OPT_EQ_PRESET = 0x2E      # EQ preset selection (0-18)
OPT_EQ = 0x31             # Custom EQ band control

# ── PipeWire Sink Names ─────────────────────────────────
GAME_SINK = "NovaGame"
CHAT_SINK = "NovaChat"
OUTPUT_MATCH = "SteelSeries_Arctis_Nova_Pro_Wireless"

# ── Retry Settings ──────────────────────────────────────
RECONNECT_INTERVAL = 3  # seconds between reconnect attempts
MAX_RECONNECT_WAIT = 30  # max wait between attempts


class NovaMixer:
    """Main controller — manages HID device and PipeWire sinks."""

    def __init__(self):
        self.dev = None
        self.game_loopback = None
        self.chat_loopback = None
        self.running = True
        self._setup_signals()

    def _setup_signals(self):
        signal.signal(signal.SIGINT, self._handle_exit)
        signal.signal(signal.SIGTERM, self._handle_exit)

    def _handle_exit(self, signum, frame):
        log.info("Shutting down...")
        self.running = False

    # ── Device Discovery ────────────────────────────────
    def _find_device(self) -> str | None:
        """Find the HID device path for the base station."""
        for dev in hid.enumerate(VENDOR_ID, PRODUCT_ID):
            if dev["interface_number"] == HID_INTERFACE:
                return dev["path"]
        return None

    def _find_output_sink(self) -> str | None:
        """Auto-detect the Nova Pro Wireless PipeWire output sink."""
        try:
            result = subprocess.run(
                ["pactl", "list", "sinks", "short"],
                capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.strip().split("\n"):
                if OUTPUT_MATCH in line:
                    return line.split("\t")[1]
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        return None

    # ── HID Communication ───────────────────────────────
    def _open_device(self) -> bool:
        """Open HID device. Returns True on success."""
        path = self._find_device()
        if not path:
            return False
        try:
            self.dev = hid.device()
            self.dev.open_path(path)
            self.dev.set_nonblocking(True)
            log.info("Base station connected")
            return True
        except OSError as e:
            log.error(f"Failed to open device: {e}")
            self.dev = None
            return False

    def _send(self, *data):
        """Send a HID message to the base station."""
        msg = list(data) + [0] * (MSG_LEN - len(data))
        try:
            self.dev.write(msg)
        except OSError:
            raise ConnectionError("Device disconnected")

    def _enable_chatmix(self):
        """Enable ChatMix mode and Sonar icon on the base station."""
        self._send(TX, OPT_CHATMIX_ENABLE, 1)
        self._send(TX, OPT_SONAR_ICON, 1)
        log.info("ChatMix enabled on base station")

    def _disable_chatmix(self):
        """Disable ChatMix mode and Sonar icon."""
        try:
            if self.dev:
                self._send(TX, OPT_CHATMIX_ENABLE, 0)
                self._send(TX, OPT_SONAR_ICON, 0)
                log.info("ChatMix disabled on base station")
        except (OSError, ConnectionError):
            pass

    # ── PipeWire Sinks ──────────────────────────────────
    def _create_sinks(self, output_sink: str):
        """Create virtual PipeWire sinks via pw-loopback."""
        self._destroy_sinks()
        self.game_loopback = subprocess.Popen([
            "pw-loopback", "-P", output_sink,
            "--capture-props=media.class=Audio/Sink",
            "-n", GAME_SINK,
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.chat_loopback = subprocess.Popen([
            "pw-loopback", "-P", output_sink,
            "--capture-props=media.class=Audio/Sink",
            "-n", CHAT_SINK,
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log.info(f"Created sinks: {GAME_SINK}, {CHAT_SINK}")

    def _destroy_sinks(self):
        """Terminate virtual sinks."""
        for proc in (self.game_loopback, self.chat_loopback):
            if proc and proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)
        self.game_loopback = None
        self.chat_loopback = None

    def _set_volume(self, sink: str, volume: int):
        """Set sink volume (0-100)."""
        subprocess.Popen(
            ["pactl", "set-sink-volume", f"input.{sink}", f"{volume}%"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    # ── Main Loop ───────────────────────────────────────
    def run(self):
        """Connect, create sinks, and process dial events."""
        log.info("nova-mixer starting...")

        while self.running:
            # Find output sink
            output_sink = self._find_output_sink()
            if not output_sink:
                log.warning("Output sink not found — is the headset connected?")
                self._wait_reconnect()
                continue

            # Open HID device
            if not self._open_device():
                log.warning("Base station not found — waiting...")
                self._wait_reconnect()
                continue

            # Set up
            try:
                self._enable_chatmix()
                self._create_sinks(output_sink)
                notify("🎧 ChatMix Active", "NovaGame and NovaChat sinks ready.\nUse the dial to control balance.")
            except ConnectionError:
                log.warning("Lost connection during setup")
                self._cleanup_device()
                continue

            # Event loop — read dial
            log.info("Listening for ChatMix dial events...")
            reconnect_needed = False
            while self.running and not reconnect_needed:
                try:
                    msg = self.dev.read(MSG_LEN, 1000)  # 1s timeout
                    if not msg:
                        continue
                    if msg[1] == OPT_CHATMIX:
                        game_vol = msg[2]
                        chat_vol = msg[3]
                        self._set_volume(GAME_SINK, game_vol)
                        self._set_volume(CHAT_SINK, chat_vol)
                except OSError:
                    log.warning("Device disconnected")
                    notify("🎧 Base Station Disconnected", "Waiting for reconnect...", "audio-headset")
                    reconnect_needed = True

            # Cleanup and retry
            self._cleanup_device()
            if self.running:
                log.info("Will attempt reconnect...")

        # Final cleanup
        self._destroy_sinks()
        log.info("nova-mixer stopped")

    def _cleanup_device(self):
        """Clean up device state."""
        self._disable_chatmix()
        self._destroy_sinks()
        if self.dev:
            try:
                self.dev.close()
            except OSError:
                pass
            self.dev = None

    def _wait_reconnect(self):
        """Wait before retrying connection."""
        waited = 0
        while self.running and waited < RECONNECT_INTERVAL:
            time.sleep(1)
            waited += 1


class DeviceNotFoundException(Exception):
    pass


if __name__ == "__main__":
    mixer = NovaMixer()
    mixer.run()
