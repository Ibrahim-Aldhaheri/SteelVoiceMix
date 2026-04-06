#!/bin/bash
set -e

echo "🎧 nova-mixer installer"
echo "========================"

# Check dependencies
echo "Checking dependencies..."
missing=""

command -v pactl >/dev/null || missing="$missing pactl(pulseaudio-utils)"

if ! command -v pw-loopback >/dev/null; then
    echo "❌ pw-loopback not found. PipeWire is required."
    echo ""
    if command -v dnf >/dev/null; then
        echo "Install with: sudo dnf install pipewire pipewire-utils"
    elif command -v apt >/dev/null; then
        echo "Install with: sudo apt install pipewire pipewire-utils"
    fi
    exit 1
fi

command -v notify-send >/dev/null || missing="$missing libnotify(libnotify)"

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
if ! command -v cargo >/dev/null; then
    echo "❌ Rust toolchain (cargo) not found."
    echo "Install from: https://rustup.rs/"
    exit 1
fi

# hidapi needs system library
if command -v dnf >/dev/null; then
    if ! rpm -q hidapi-devel &>/dev/null; then
        echo "Installing hidapi-devel..."
        sudo dnf install -y hidapi-devel
    fi
elif command -v apt >/dev/null; then
    if ! dpkg -s libhidapi-dev &>/dev/null 2>&1; then
        echo "Installing libhidapi-dev..."
        sudo apt install -y libhidapi-dev
    fi
fi

cargo build --release
echo "✅ Build successful"

# Install udev rules
echo "Installing udev rules..."
sudo cp 50-nova-pro-wireless.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger
echo "✅ udev rules installed"

# Install binary and GUI
echo "Installing nova-mixer..."
mkdir -p ~/.local/bin ~/.local/lib/nova-mixer

cp target/release/nova-mixer ~/.local/bin/nova-mixer
cp nova-mixer-gui.py ~/.local/lib/nova-mixer/nova-mixer-gui.py

# Create GUI launcher
cat > ~/.local/bin/nova-mixer-gui << 'LAUNCHER'
#!/bin/bash
exec python3 "$HOME/.local/lib/nova-mixer/nova-mixer-gui.py" "$@"
LAUNCHER
chmod +x ~/.local/bin/nova-mixer-gui
echo "✅ Installed to ~/.local/bin/nova-mixer"

# Install systemd service
echo "Installing systemd service..."
mkdir -p ~/.config/systemd/user
cp nova-mixer.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable nova-mixer --now
echo "✅ Service enabled and started"

echo ""
echo "🎮 nova-mixer is running!"
echo "   NovaGame and NovaChat sinks should now appear in your audio settings."
echo "   Route your game audio to NovaGame and Discord to NovaChat."
echo ""
echo "   GUI:          nova-mixer-gui"
echo "   Check status: systemctl --user status nova-mixer"
echo "   View logs:    journalctl --user -u nova-mixer -f"
echo "   Stop:         systemctl --user stop nova-mixer"
echo "   Disable:      systemctl --user disable nova-mixer"
