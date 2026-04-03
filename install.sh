#!/bin/bash
set -e

echo "🎧 nova-mixer installer"
echo "========================"

# Check dependencies
echo "Checking dependencies..."
missing=""
command -v python3 >/dev/null || missing="$missing python3"
command -v pactl >/dev/null || missing="$missing pactl(pulseaudio-utils)"
command -v pw-loopback >/dev/null || missing="$missing pw-loopback(pipewire)"
python3 -c "import hid" 2>/dev/null || missing="$missing python3-hidapi"
command -v notify-send >/dev/null || missing="$missing libnotify(libnotify)"

if [ -n "$missing" ]; then
    echo "❌ Missing:$missing"
    echo ""
    if command -v dnf >/dev/null; then
        echo "Install with: sudo dnf install pulseaudio-utils python3 python3-hidapi libnotify"
        echo "For GUI:      sudo dnf install python3-pyside6"
    elif command -v apt >/dev/null; then
        echo "Install with: sudo apt install pulseaudio-utils python3 python3-hid libnotify-bin"
        echo "For GUI:      pip install PySide6"
    fi
    exit 1
fi

echo "✅ All dependencies found"

# Install udev rules
echo "Installing udev rules..."
sudo cp 50-nova-pro-wireless.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger
echo "✅ udev rules installed"

# Install script
echo "Installing nova-mixer..."
mkdir -p ~/.local/bin ~/.local/lib/nova-mixer
cp nova_mixer_core.py ~/.local/lib/nova-mixer/
cp nova-mixer-gui.py ~/.local/lib/nova-mixer/nova_mixer_gui.py
cp nova-mixer.py ~/.local/lib/nova-mixer/

# Create launcher script
cat > ~/.local/bin/nova-mixer << 'LAUNCHER'
#!/bin/bash
SCRIPT_DIR="$HOME/.local/lib/nova-mixer"
exec python3 "$SCRIPT_DIR/nova-mixer.py" "$@"
LAUNCHER
chmod +x ~/.local/bin/nova-mixer
echo "✅ Installed to ~/.local/bin/nova-mixer"
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
echo "   The service auto-starts on login and when the base station is plugged in."
echo "   It auto-reconnects if the device disconnects or sleeps."
echo ""
echo "   Check status: systemctl --user status nova-mixer"
echo "   View logs:    journalctl --user -u nova-mixer -f"
echo "   Stop:         systemctl --user stop nova-mixer"
echo "   Disable:      systemctl --user disable nova-mixer"
