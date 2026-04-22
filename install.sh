#!/bin/bash
###############################################################################
#                              DISCLAIMER                                     #
#                        USE AT YOUR OWN RISK                                 #
# steelvoicemix has NO ASSOCIATION with SteelSeries.                          #
# The author is NOT RESPONSIBLE for bricked devices, voided warranties,       #
# or any damage caused by this software.                                      #
# If your base station starts playing elevator music, that's between you      #
# and the universe.                                                           #
###############################################################################
set -e

# Parse arguments
DEBUG="${DEBUG:-0}"
INSTALL_GUI=1
while [[ $# -gt 0 ]]; do
    case $1 in
        --debug)
            DEBUG=1
            shift
            ;;
        --no-gui)
            INSTALL_GUI=0
            shift
            ;;
        --help|-h)
            cat <<'USAGE'
Usage: ./install.sh [--debug] [--no-gui]

  --debug    Verbose logging during install
  --no-gui   Install only the headless daemon (skip Qt GUI, tray, overlay,
             and the steelvoicemix.desktop menu entry). Intended for GNOME
             without tray extensions, Sway/i3, and headless servers.
USAGE
            exit 0
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

echo "⚠️  This script will install steelvoicemix on your system."
echo "   steelvoicemix has NO ASSOCIATION with SteelSeries."
echo "   The author is NOT RESPONSIBLE for any damage caused by this software."
echo ""
echo "Press Ctrl+C to cancel, or Enter to continue..."
read -r

echo ""
echo "🎧 steelvoicemix installer"
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
echo "Building SteelVoiceMix..."
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
echo "Installing SteelVoiceMix..."
mkdir -p ~/.local/bin ~/.local/lib/steelvoicemix

debug "Copying: target/release/steelvoicemix → ~/.local/bin/steelvoicemix"
cp target/release/steelvoicemix ~/.local/bin/steelvoicemix
if [ "$INSTALL_GUI" = "1" ]; then
    debug "Copying: steelvoicemix-gui.py → ~/.local/lib/steelvoicemix/"
    cp steelvoicemix-gui.py ~/.local/lib/steelvoicemix/steelvoicemix-gui.py
    debug "Copying: gui/ package → ~/.local/lib/steelvoicemix/gui/"
    rm -rf ~/.local/lib/steelvoicemix/gui
    cp -r gui ~/.local/lib/steelvoicemix/gui

    # Create GUI launcher. Forcing XCB keeps overlay positioning working on
    # Wayland sessions: KWin (and wlroots, mutter) ignore client-side move()
    # calls on ordinary widgets, which would pin the overlay to screen centre
    # no matter what the user picks. XWayland gives us X11 semantics with
    # zero behavioural cost for a tray + overlay app.
    cat > ~/.local/bin/steelvoicemix-gui << 'LAUNCHER'
#!/bin/bash
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-xcb}"
exec python3 "$HOME/.local/lib/steelvoicemix/steelvoicemix-gui.py" "$@"
LAUNCHER
    chmod +x ~/.local/bin/steelvoicemix-gui
    echo "✅ Installed daemon + GUI to ~/.local/bin/steelvoicemix{,-gui}"
else
    echo "✅ Installed daemon to ~/.local/bin/steelvoicemix (GUI skipped per --no-gui)"
fi

if [ "$INSTALL_GUI" = "1" ]; then
    # Register the GUI in the user's app menu (KDE/GNOME will pick it up)
    echo "Registering application menu entry..."
    mkdir -p ~/.local/share/applications
    cp steelvoicemix.desktop ~/.local/share/applications/
    if command -v update-desktop-database >/dev/null; then
        debug "Running: update-desktop-database ~/.local/share/applications"
        update-desktop-database ~/.local/share/applications 2>/dev/null || true
    fi
    echo "✅ Menu entry installed"

    # Install the app icon into the user's hicolor theme so KDE/GNOME pick it up
    echo "Installing app icon..."
    mkdir -p ~/.local/share/icons/hicolor/scalable/apps
    cp data/icons/hicolor/scalable/apps/steelvoicemix.svg ~/.local/share/icons/hicolor/scalable/apps/
    if command -v gtk-update-icon-cache >/dev/null; then
        debug "Running: gtk-update-icon-cache ~/.local/share/icons/hicolor"
        gtk-update-icon-cache -f ~/.local/share/icons/hicolor 2>/dev/null || true
    fi
    echo "✅ Icon installed"

    # Clean up any stale .desktop autostart from previous installs
    rm -f ~/.config/autostart/steelvoicemix-gui.desktop

    # Install the GUI as a user service bound to the graphical session.
    # The master unit ships with ExecStart=/usr/bin/steelvoicemix-gui for RPM
    # installs; rewrite to the per-user bin path for this source install.
    echo "Installing GUI autostart service..."
    debug "Copying: steelvoicemix-gui.service → ~/.config/systemd/user/"
    cp steelvoicemix-gui.service ~/.config/systemd/user/
    sed -i "s|^ExecStart=/usr/bin/|ExecStart=$HOME/.local/bin/|" \
        ~/.config/systemd/user/steelvoicemix-gui.service
    systemctl --user daemon-reload
    debug "Running: systemctl --user enable steelvoicemix-gui"
    systemctl --user enable steelvoicemix-gui
    # Start it now if a graphical session is active so the user doesn't have to
    # log out and back in.
    if systemctl --user is-active --quiet graphical-session.target; then
        debug "Running: systemctl --user start steelvoicemix-gui"
        systemctl --user start steelvoicemix-gui 2>/dev/null || true
    fi
    echo "✅ GUI will start automatically on login"
else
    # Headless install — tear down any GUI bits from a previous install so
    # a repeat run of `--no-gui` ends in a clean state.
    systemctl --user stop steelvoicemix-gui 2>/dev/null || true
    systemctl --user disable steelvoicemix-gui 2>/dev/null || true
    rm -f ~/.config/systemd/user/steelvoicemix-gui.service
    rm -f ~/.local/bin/steelvoicemix-gui
    rm -rf ~/.local/lib/steelvoicemix/gui
    rm -f ~/.local/share/applications/steelvoicemix.desktop
    rm -f ~/.config/autostart/steelvoicemix-gui.desktop
    systemctl --user daemon-reload 2>/dev/null || true
    echo "✅ Headless install — GUI, menu entry, and autostart service skipped"
fi

# Install systemd service
echo "Installing systemd service..."
mkdir -p ~/.config/systemd/user
debug "Copying: steelvoicemix.service → ~/.config/systemd/user/"
cp steelvoicemix.service ~/.config/systemd/user/
sed -i "s|^ExecStart=/usr/bin/|ExecStart=$HOME/.local/bin/|" \
    ~/.config/systemd/user/steelvoicemix.service
debug "Running: systemctl --user daemon-reload"
systemctl --user daemon-reload
debug "Running: systemctl --user enable steelvoicemix --now"
systemctl --user enable steelvoicemix --now
echo "✅ Service enabled and started"

echo ""
echo "==========================================="
echo "🎉 Installation complete!"
echo "==========================================="
echo ""
echo "📦 Installed components:"
echo "   • steelvoicemix daemon    → ~/.local/bin/steelvoicemix"
if [ "$INSTALL_GUI" = "1" ]; then
    echo "   • steelvoicemix GUI       → ~/.local/bin/steelvoicemix-gui"
fi
echo "   • systemd service         → ~/.config/systemd/user/steelvoicemix.service"
echo "   • udev rules              → /etc/udev/rules.d/50-nova-pro-wireless.rules"
echo ""
echo "🎮 steelvoicemix is running!"
echo "   SteelGame and SteelChat sinks should now appear in your audio settings."
echo "   Route your game audio to SteelGame and Discord to SteelChat."
echo ""
echo "📋 Quick reference:"
if [ "$INSTALL_GUI" = "1" ]; then
    echo "   GUI:          steelvoicemix-gui"
fi
echo "   Check status: systemctl --user status steelvoicemix"
echo "   View logs:    journalctl --user -u steelvoicemix -f"
echo "   Stop:         systemctl --user stop steelvoicemix"
echo "   Disable:      systemctl --user disable steelvoicemix"
echo "   Uninstall:    ./uninstall.sh"
