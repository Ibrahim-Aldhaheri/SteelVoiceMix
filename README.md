# nova-mixer 🎧

Linux ChatMix implementation for the **SteelSeries Arctis Nova Pro Wireless**. Uses PipeWire virtual sinks controlled by the hardware dial on the base station.

Replaces the ChatMix functionality of SteelSeries Sonar (Windows-only) on Linux.

## Features

- 🎮 **ChatMix dial support** — physical dial controls Game/Chat audio balance
- 🔊 **PipeWire virtual sinks** — creates NovaGame and NovaChat sinks automatically
- 🔌 **Plug and play** — auto-detects the base station
- 🖥️ **KDE/GNOME compatible** — sinks appear in system audio settings
- 🐧 **Systemd service** — runs on boot, no manual startup needed

## How It Works

The Arctis Nova Pro Wireless base station communicates via USB HID. This tool:

1. Sends HID commands to enable ChatMix mode on the base station
2. Creates two virtual PipeWire sinks (Game + Chat)
3. Listens for dial position changes and adjusts sink volumes in real-time

Route your game audio to **NovaGame** and Discord/comms to **NovaChat** — the dial does the rest.

## Requirements

- SteelSeries Arctis Nova Pro Wireless (base station connected via USB)
- PipeWire (default on Fedora 34+, Ubuntu 22.10+)
- Python 3.8+
- `python-hidapi`, `pactl`

## Installation

### Fedora
```bash
sudo dnf install pulseaudio-utils python3 python3-hidapi
```

### Ubuntu/Debian
```bash
sudo apt install pulseaudio-utils python3 python3-hid
```

### Setup

```bash
git clone https://github.com/Ibrahim-Aldhaheri/nova-mixer.git
cd nova-mixer

# udev rules (required for non-root access)
sudo cp 50-nova-pro-wireless.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger

# Install as systemd user service (auto-start on login)
mkdir -p ~/.local/bin ~/.config/systemd/user
cp nova-chatmix.py ~/.local/bin/nova-mixer
cp nova-mixer.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable nova-mixer --now
```

## Usage

```bash
# Headless daemon (default)
nova-mixer

# With GUI monitor
nova-mixer --gui

# Disable desktop notifications
nova-mixer --no-notify

# Check service status
systemctl --user status nova-mixer
```

Once running, two new audio sinks appear:
- **NovaGame** — route games, music, browser here
- **NovaChat** — route Discord, TeamSpeak, etc. here

The physical dial on the base station controls the balance between them.

### GUI

The `--gui` flag opens a small monitor window showing:
- Connection status (connected/disconnected)
- Game and Chat volume bars (updated in real-time as you turn the dial)
- Dial position indicator (Game-heavy, Chat-heavy, or Balanced)

The window minimizes to the system tray — click the tray icon to reopen it. Closing the window hides it to tray instead of quitting.

**Extra dependency for GUI:**
```bash
sudo dnf install python3-pyside6   # Fedora/KDE
```

## Disclaimer

⚠️ **USE AT YOUR OWN RISK.** This project has no association with SteelSeries. The author is not responsible for any damage to your hardware, bricked devices, or voided warranties. If your base station starts playing elevator music on its own, that's between you and the universe.

## Acknowledgments

Inspired by [nova-chatmix-linux](https://git.dymstro.nl/Dymstro/nova-chatmix-linux) by Dymstro, who reverse-engineered the Arctis Nova Pro Wireless USB HID protocol.

## License

[MIT](LICENSE)
