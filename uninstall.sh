#!/bin/bash
set -e

echo "🎧 nova-mixer uninstaller"
echo "========================="

# Stop and disable services
echo "Stopping services..."
systemctl --user stop nova-mixer-gui 2>/dev/null || true
systemctl --user disable nova-mixer-gui 2>/dev/null || true
systemctl --user stop nova-mixer 2>/dev/null || true
systemctl --user disable nova-mixer 2>/dev/null || true

# Remove files
echo "Removing files..."
rm -f ~/.local/bin/nova-mixer
rm -f ~/.local/bin/nova-mixer-gui
rm -rf ~/.local/lib/nova-mixer
rm -f ~/.config/systemd/user/nova-mixer.service
rm -f ~/.config/systemd/user/nova-mixer-gui.service
rm -rf ~/.config/nova-mixer
rm -f ~/.local/share/applications/nova-mixer.desktop
rm -f ~/.config/autostart/nova-mixer-gui.desktop
if command -v update-desktop-database >/dev/null; then
    update-desktop-database ~/.local/share/applications 2>/dev/null || true
fi

# Reload systemd
systemctl --user daemon-reload 2>/dev/null || true

# Remove udev rules
if [ -f /etc/udev/rules.d/50-nova-pro-wireless.rules ]; then
    echo "Removing udev rules (needs sudo)..."
    sudo rm -f /etc/udev/rules.d/50-nova-pro-wireless.rules
    sudo udevadm control --reload-rules
    sudo udevadm trigger
fi

echo ""
echo "✅ nova-mixer uninstalled"
