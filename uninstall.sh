#!/bin/bash
set -e

echo "🎧 SteelVoiceMix uninstaller"
echo "========================="

# Stop and disable services
echo "Stopping services..."
systemctl --user stop steelvoicemix-gui 2>/dev/null || true
systemctl --user disable steelvoicemix-gui 2>/dev/null || true
systemctl --user stop steelvoicemix 2>/dev/null || true
systemctl --user disable steelvoicemix 2>/dev/null || true

# Kill any manually-launched processes that systemd doesn't know about
pkill -f steelvoicemix-gui.py 2>/dev/null || true
pkill -x steelvoicemix 2>/dev/null || true

# Unload any of our null-sink / loopback modules left behind in PipeWire.
# The daemon normally unloads them on shutdown, but a crash or manual
# test can orphan them — they'd otherwise linger until the user logs
# out or manually runs pactl unload-module. The pattern matches both the
# current Steel* sink names and the legacy Nova* ones so users upgrading
# from earlier installs get swept clean too.
if command -v pactl >/dev/null; then
    pactl list modules 2>/dev/null \
        | awk '/^Module #/ {id=$2; sub("#", "", id)} /^\tArgument:.*(sink_name=(Steel|Nova)|source=(Steel|Nova))/ {print id}' \
        | xargs -r -n1 pactl unload-module 2>/dev/null || true
fi

# Remove files
echo "Removing files..."
rm -f ~/.local/bin/steelvoicemix
rm -f ~/.local/bin/steelvoicemix-gui
rm -rf ~/.local/lib/steelvoicemix
rm -f ~/.config/systemd/user/steelvoicemix.service
rm -f ~/.config/systemd/user/steelvoicemix-gui.service
rm -rf ~/.config/steelvoicemix
rm -f ~/.local/share/applications/steelvoicemix.desktop
rm -f ~/.local/share/icons/hicolor/scalable/apps/steelvoicemix.svg
if command -v gtk-update-icon-cache >/dev/null; then
    gtk-update-icon-cache -f ~/.local/share/icons/hicolor 2>/dev/null || true
fi
rm -f ~/.config/autostart/steelvoicemix-gui.desktop
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
echo "✅ SteelVoiceMix uninstalled"
