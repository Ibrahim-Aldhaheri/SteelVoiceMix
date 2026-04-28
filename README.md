# SteelVoiceMix 🎧

[![COPR build status](https://copr.fedorainfracloud.org/coprs/abokhalil/steelvoicemix/package/steelvoicemix/status_image/last_build.png)](https://copr.fedorainfracloud.org/coprs/abokhalil/steelvoicemix/package/steelvoicemix/)

> ## Acknowledgments
>
> SteelVoiceMix builds on the work of three upstream projects whose protocol
> decoding, sink-routing patterns, and feature designs informed this codebase:
>
> - [**nova-chatmix-linux**](https://github.com/Dymstro/nova-chatmix-linux) by **Dymstro** — reverse-engineered the SteelSeries Arctis Nova Pro Wireless USB HID protocol. Without their work, none of the Linux-side ChatMix tooling for this device would exist.
> - [**Linux-Arctis-Manager**](https://github.com/elegos/Linux-Arctis-Manager) by **elegos** — the original open-source Linux ChatMix *manager* for the SteelSeries Arctis line. The HID command set, ChatMix dial decoding, and the `module-null-sink` + `module-loopback` virtual-sink pattern all originate from this project.
> - [**Arctis Sound Manager**](https://github.com/loteran/Arctis-Sound-Manager) by **loteran** — a comprehensive fork of Linux-Arctis-Manager that pioneered the multi-channel mixer (Game / Chat / Media / HDMI), Sonar-style parametric EQ, OLED weather widget, and the HeSuVi-based spatial audio pipeline. SteelVoiceMix's multi-channel and OLED enhancements were designed with reference to ASM.
>
> If you want **full SteelSeries Sonar parity** — 312 game EQ presets, audio profiles, multi-language UI, broad device coverage — use **ASM** directly. SteelVoiceMix is a Rust-daemon alternative narrower in scope and Fedora-KDE-first.

Linux ChatMix implementation for the **SteelSeries Arctis Nova Pro Wireless**. Uses PipeWire virtual sinks controlled by the hardware dial on the base station.

Replaces the ChatMix functionality of SteelSeries Sonar (Windows-only) on Linux.

## Screenshots

<p align="center">
  <img src="screenshots/main-window.png" alt="Main window — connection status, Game/Chat volumes, battery, and overlay settings" width="420" />
</p>
<p align="center">
  <img src="screenshots/dial-overlay.png" alt="Dial overlay — vertical style, flashes briefly when the hardware dial is turned" width="140" />
</p>

## Debug Mode

Run the installer with verbose output to troubleshoot issues:

```bash
DEBUG=1 ./install.sh
# or
./install.sh --debug
```

## Features

- 🎮 **ChatMix dial support** — physical dial controls Game/Chat audio balance
- 🔊 **PipeWire virtual sinks** — creates SteelGame and SteelChat sinks automatically
- 🔋 **Battery monitoring** — polls battery level and charging status
- 🔌 **Plug and play** — auto-detects the base station, auto-reconnects with exponential backoff
- 🖥️ **KDE/GNOME compatible** — sinks appear in system audio settings
- 🐧 **Systemd service** — runs on boot, no manual startup needed
- 🖼️ **Optional GUI** — PySide6 system tray app with overlay, battery display

## Architecture

The project is split into two parts:

1. **Rust daemon** (`steelvoicemix`) — handles HID communication, creates PipeWire sinks, reads the ChatMix dial, adjusts volumes. Runs as a systemd user service.
2. **Python GUI** (`steelvoicemix-gui`) — optional PySide6 app that connects to the daemon over a Unix socket (`$XDG_RUNTIME_DIR/steelvoicemix.sock`) for real-time status display.

### Socket Protocol

The daemon exposes a JSON-over-Unix-socket API:

```json
// Client → Daemon
{"cmd": "subscribe"}     // Stream all events
{"cmd": "status"}        // One-shot status query

// Daemon → Client (events)
{"event": "chatmix", "game": 80, "chat": 60}
{"event": "battery", "level": 75, "status": "active"}
{"event": "connected"}
{"event": "disconnected"}
{"event": "status", "connected": true, "game_vol": 80, "chat_vol": 60, "battery": {"level": 75, "status": "active"}}
```

## How It Works

The Arctis Nova Pro Wireless base station communicates via USB HID. This tool:

1. Sends HID commands to enable ChatMix mode on the base station
2. Creates two virtual PipeWire sinks (Game + Chat) via `pw-loopback`
3. Listens for dial position changes and adjusts sink volumes in real-time via `pactl`
4. Polls battery status every 60 seconds

Route your game audio to **SteelGame** and Discord/comms to **SteelChat** — the dial does the rest.

## Requirements

- SteelSeries Arctis Nova Pro Wireless (base station connected via USB)
- PipeWire (default on Fedora 34+, Ubuntu 22.10+)
- Rust toolchain (for building)
- `pactl`, `pw-loopback`, `hidapi`

## Installation

### Build Dependencies

```bash
# Fedora
sudo dnf install cargo hidapi-devel pulseaudio-utils libnotify

# Ubuntu/Debian
sudo apt install cargo libhidapi-dev pulseaudio-utils libnotify-bin
```

### Fedora (COPR — recommended)

```bash
sudo dnf copr enable abokhalil/steelvoicemix
sudo dnf install steelvoicemix
systemctl --user daemon-reload
systemctl --user enable --now steelvoicemix steelvoicemix-gui
```

### From source

```bash
git clone https://github.com/Ibrahim-Aldhaheri/SteelVoiceMix.git
cd SteelVoiceMix
./install.sh
```

The install script will:
1. Check runtime dependencies (fails if `pw-loopback` is missing)
2. Build the Rust binary with `cargo build --release`
3. Install udev rules for non-root HID access
4. Install the binary to `~/.local/bin/`
5. Enable the systemd user service

**Headless install (no GUI, tray, or overlay)** — for GNOME without tray extensions, Sway/i3, or servers:

```bash
./install.sh --no-gui
```

Only the daemon, systemd service, and udev rule get installed. The ChatMix dial still works end-to-end; you manage audio routing from your DE's audio settings or `pavucontrol`.

### Manual Install

```bash
cargo build --release

# udev rules
sudo cp 50-nova-pro-wireless.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger

# Binary
cp target/release/steelvoicemix ~/.local/bin/

# Systemd service
mkdir -p ~/.config/systemd/user
cp steelvoicemix.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable steelvoicemix --now
```

## Usage

```bash
# Headless daemon (default)
steelvoicemix

# Disable desktop notifications
steelvoicemix --no-notify

# Disable Unix socket (no GUI support)
steelvoicemix --no-socket

# Launch GUI (requires PySide6, daemon must be running)
steelvoicemix-gui

# Check service status
systemctl --user status steelvoicemix

# View logs
journalctl --user -u steelvoicemix -f
```

Once running, two new audio sinks appear:
- **SteelGame** — route games, music, browser here
- **SteelChat** — route Discord, TeamSpeak, etc. here

The physical dial on the base station controls the balance between them.

### GUI

The GUI (`steelvoicemix-gui`) connects to the running daemon and shows:
- Connection status (connected/disconnected)
- Game and Chat volume bars (updated in real-time)
- Battery level and charging status
- Dial position indicator (Game-heavy, Chat-heavy, or Balanced)
- Floating overlay on dial turn

The window minimizes to the system tray.

**Extra dependency for GUI:**
```bash
sudo dnf install python3-pyside6   # Fedora/KDE
pip install PySide6                 # pip
```

## Uninstall

```bash
./uninstall.sh
```

## Disclaimer

⚠️ **USE AT YOUR OWN RISK.** This project has no association with SteelSeries. The author is not responsible for any damage to your hardware, bricked devices, or voided warranties. If your base station starts playing elevator music on its own, that's between you and the universe.

🧪 **Tested on Fedora KDE only.** Other distributions and desktop environments may work but haven't been verified. If you run it elsewhere and hit problems, please open an issue with your setup details.

## License

[GPL-3.0-or-later](LICENSE).

SteelVoiceMix is licensed under the GNU General Public License, version 3 or
later, in alignment with the broader Linux audio ecosystem (PipeWire is LGPL,
JACK is LGPL, the GNU userland is mostly GPL) and the upstream projects whose
work made this possible (Linux-Arctis-Manager and Arctis Sound Manager are
both GPL-3.0). If you fork or build on this code, your derivative work must
also be GPL-3.0-compatible.
