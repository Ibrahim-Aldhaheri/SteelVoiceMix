#!/bin/bash
###############################################################################
#                              DISCLAIMER                                     #
#                        USE AT YOUR OWN RISK                                 #
# nova-mixer has NO ASSOCIATION with SteelSeries.                             #
# The author is NOT RESPONSIBLE for bricked devices, voided warranties,       #
# or any damage caused by this software.                                      #
# If your base station starts playing elevator music, that's between you      #
# and the universe.                                                           #
###############################################################################
set -e

# Parse arguments
DEBUG="${DEBUG:-0}"
while [[ $# -gt 0 ]]; do
    case $1 in
        --debug)
            DEBUG=1
            shift
            ;;
        *)
            shift
            ;;
    esac
done

debug() {
    if [ "$DEBUG" = "1" ]; then
        echo "[DEBUG] $*"
    fi
}

echo "⚠️  This script will install nova-mixer on your system."
echo "   nova-mixer has NO ASSOCIATION with SteelSeries."
echo "   The author is NOT RESPONSIBLE for any damage caused by this software."
echo ""
echo "Press Ctrl+C to cancel, or Enter to continue..."
read -r

echo ""
echo "🎧 nova-mixer installer"
echo "========================"

if [ "$DEBUG" = "1" ]; then
    echo "[DEBUG] Debug mode enabled"
fi

# Check dependencies
echo "Checking dependencies..."
missing=""

debug "Checking dependency: pactl"
if command -v pactl >/dev/null; then
    debug "  ✅ pactl found at $(command -v pactl)"
else
    debug "  ❌ pactl not found"
    missing="$missing pactl(pulseaudio-utils)"
fi

debug "Checking dependency: pw-loopback"
if ! command -v pw-loopback >/dev/null; then
    debug "  ❌ pw-loopback not found"
    echo "❌ pw-loopback not found. PipeWire is required."
    echo ""
    if command -v dnf >/dev/null; then
        echo "Install with: sudo dnf install pipewire pipewire-utils"
    elif command -v apt >/dev/null; then
        echo "Install with: sudo apt install pipewire pipewire-utils"
    fi
    exit 1
else
    debug "  ✅ pw-loopback found at $(command -v pw-loopback)"
fi

debug "Checking dependency: notify-send"
if command -v notify-send >/dev/null; then
    debug "  ✅ notify-send found at $(command -v notify-send)"
else
    debug "  ❌ notify-send not found"
    missing="$missing libnotify(libnotify)"
fi

if [ -n "$missing" ]; then
    echo "❌ Missing:$missing"
    echo ""
    if command -v dnf >/dev/null; then
        echo "Install with: sudo dnf install pulseaudio-utils libnotify"
    elif command -v apt >/dev/null; then
        echo "Install with: sudo apt install pulseaudio-utils libnotify-bin"
    fi
    exit 1
fi

echo "✅ All runtime dependencies found"

# Build Rust binary
echo "Building nova-mixer..."
debug "Checking for cargo"
if ! command -v cargo >/dev/null; then
    echo "❌ Rust toolchain (cargo) not found."
    echo "Install from: https://rustup.rs/"
    exit 1
fi
debug "  ✅ cargo found at $(command -v cargo)"

# hidapi needs system library
if command -v dnf >/dev/null; then
    if ! rpm -q hidapi-devel &>/dev/null; then
        echo "Installing hidapi-devel..."
        debug "Running: sudo dnf install -y hidapi-devel"
        sudo dnf install -y hidapi-devel
    fi
elif command -v apt >/dev/null; then
    if ! dpkg -s libhidapi-dev &>/dev/null 2>&1; then
        echo "Installing libhidapi-dev..."
        debug "Running: sudo apt install -y libhidapi-dev"
        sudo apt install -y libhidapi-dev
    fi
fi

debug "Running: cargo build --release"
cargo build --release
echo "✅ Build successful"

# Install udev rules
echo "Installing udev rules..."
debug "Copying: 50-nova-pro-wireless.rules → /etc/udev/rules.d/"
sudo cp 50-nova-pro-wireless.rules /etc/udev/rules.d/
debug "Running: sudo udevadm control --reload-rules"
sudo udevadm control --reload-rules
debug "Running: sudo udevadm trigger"
sudo udevadm trigger
echo "✅ udev rules installed"

# Install binary and GUI
echo "Installing nova-mixer..."
mkdir -p ~/.local/bin ~/.local/lib/nova-mixer

debug "Copying: target/release/nova-mixer → ~/.local/bin/nova-mixer"
cp target/release/nova-mixer ~/.local/bin/nova-mixer
debug "Copying: nova-mixer-gui.py → ~/.local/lib/nova-mixer/nova-mixer-gui.py"
cp nova-mixer-gui.py ~/.local/lib/nova-mixer/nova-mixer-gui.py

# Create GUI launcher
cat > ~/.local/bin/nova-mixer-gui << 'LAUNCHER'
#!/bin/bash
exec python3 "$HOME/.local/lib/nova-mixer/nova-mixer-gui.py" "$@"
LAUNCHER
chmod +x ~/.local/bin/nova-mixer-gui
echo "✅ Installed to ~/.local/bin/nova-mixer"

# Register the GUI in the user's app menu (KDE/GNOME will pick it up)
echo "Registering application menu entry..."
mkdir -p ~/.local/share/applications
cp nova-mixer.desktop ~/.local/share/applications/
if command -v update-desktop-database >/dev/null; then
    debug "Running: update-desktop-database ~/.local/share/applications"
    update-desktop-database ~/.local/share/applications 2>/dev/null || true
fi
echo "✅ Menu entry installed"

# Autostart the GUI on login (user can remove this file to disable)
echo "Enabling GUI autostart on login..."
mkdir -p ~/.config/autostart
cat > ~/.config/autostart/nova-mixer-gui.desktop << 'AUTOSTART'
[Desktop Entry]
Type=Application
Name=Nova Mixer GUI
Comment=ChatMix tray app for Nova Pro Wireless
Exec=nova-mixer-gui
Icon=audio-headset
Terminal=false
X-GNOME-Autostart-enabled=true
NoDisplay=true
AUTOSTART
echo "✅ Autostart enabled (remove ~/.config/autostart/nova-mixer-gui.desktop to disable)"

# Install systemd service
echo "Installing systemd service..."
mkdir -p ~/.config/systemd/user
debug "Copying: nova-mixer.service → ~/.config/systemd/user/"
cp nova-mixer.service ~/.config/systemd/user/
debug "Running: systemctl --user daemon-reload"
systemctl --user daemon-reload
debug "Running: systemctl --user enable nova-mixer --now"
systemctl --user enable nova-mixer --now
echo "✅ Service enabled and started"

echo ""
echo "==========================================="
echo "🎉 Installation complete!"
echo "==========================================="
echo ""
echo "📦 Installed components:"
echo "   • nova-mixer daemon    → ~/.local/bin/nova-mixer"
echo "   • nova-mixer GUI       → ~/.local/bin/nova-mixer-gui"
echo "   • systemd service      → ~/.config/systemd/user/nova-mixer.service"
echo "   • udev rules           → /etc/udev/rules.d/50-nova-pro-wireless.rules"
echo ""
echo "🎮 nova-mixer is running!"
echo "   NovaGame and NovaChat sinks should now appear in your audio settings."
echo "   Route your game audio to NovaGame and Discord to NovaChat."
echo ""
echo "📋 Quick reference:"
echo "   GUI:          nova-mixer-gui"
echo "   Check status: systemctl --user status nova-mixer"
echo "   View logs:    journalctl --user -u nova-mixer -f"
echo "   Stop:         systemctl --user stop nova-mixer"
echo "   Disable:      systemctl --user disable nova-mixer"
echo "   Uninstall:    ./uninstall.sh"
