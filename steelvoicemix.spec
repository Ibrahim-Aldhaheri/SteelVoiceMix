Name:           steelvoicemix
Version:        0.2.0
Release:        1%{?dist}
Summary:        ChatMix for SteelSeries Arctis Nova Pro Wireless on Linux

License:        MIT
URL:            https://github.com/Ibrahim-Aldhaheri/SteelVoiceMix
Source0:        https://github.com/Ibrahim-Aldhaheri/SteelVoiceMix/archive/refs/tags/v%{version}.tar.gz

BuildRequires:  rust >= 1.70
BuildRequires:  cargo
BuildRequires:  hidapi-devel
BuildRequires:  systemd-rpm-macros

Requires:       pipewire
Requires:       pulseaudio-utils
Requires:       libnotify
Requires:       hidapi

Recommends:     python3-pyside6

%description
Linux ChatMix implementation for the SteelSeries Arctis Nova Pro Wireless.
Rust daemon that creates virtual PipeWire sinks (NovaGame/NovaChat) controlled
by the hardware dial on the base station. Includes optional PySide6 GUI monitor
with battery indicator that communicates with the daemon over a Unix socket.

%prep
%autosetup -n SteelVoiceMix-%{version}

%build
cargo build --release

%install
# Daemon binary
install -Dm755 target/release/steelvoicemix %{buildroot}%{_bindir}/steelvoicemix

# GUI entry shim + package
install -Dm644 steelvoicemix-gui.py %{buildroot}%{_datadir}/%{name}/steelvoicemix-gui.py
install -d %{buildroot}%{_datadir}/%{name}/gui
install -Dm644 gui/*.py %{buildroot}%{_datadir}/%{name}/gui/

# GUI launcher — force XCB so overlay positioning works under Wayland
cat > %{buildroot}%{_bindir}/steelvoicemix-gui << 'EOF'
#!/bin/bash
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-xcb}"
exec python3 %{_datadir}/steelvoicemix/steelvoicemix-gui.py "$@"
EOF
chmod 755 %{buildroot}%{_bindir}/steelvoicemix-gui

# Systemd user services
install -Dm644 steelvoicemix.service %{buildroot}%{_userunitdir}/steelvoicemix.service
install -Dm644 steelvoicemix-gui.service %{buildroot}%{_userunitdir}/steelvoicemix-gui.service

# udev rules
install -Dm644 50-nova-pro-wireless.rules %{buildroot}%{_udevrulesdir}/50-nova-pro-wireless.rules

# Desktop file
install -Dm644 steelvoicemix.desktop %{buildroot}%{_datadir}/applications/steelvoicemix.desktop

# AppStream metadata
install -Dm644 dev.ibrahimaldhaheri.steelvoicemix.metainfo.xml %{buildroot}%{_metainfodir}/dev.ibrahimaldhaheri.steelvoicemix.metainfo.xml

# App icon (hicolor/scalable)
install -Dm644 data/icons/hicolor/scalable/apps/steelvoicemix.svg %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/steelvoicemix.svg

%post
%systemd_user_post steelvoicemix.service
%systemd_user_post steelvoicemix-gui.service
udevadm control --reload-rules 2>/dev/null || :
udevadm trigger 2>/dev/null || :

%preun
%systemd_user_preun steelvoicemix.service
%systemd_user_preun steelvoicemix-gui.service

%postun
udevadm control --reload-rules 2>/dev/null || :

%files
%license LICENSE
%doc README.md
%{_bindir}/steelvoicemix
%{_bindir}/steelvoicemix-gui
%{_datadir}/%{name}/
%{_userunitdir}/steelvoicemix.service
%{_userunitdir}/steelvoicemix-gui.service
%{_udevrulesdir}/50-nova-pro-wireless.rules
%{_datadir}/applications/steelvoicemix.desktop
%{_metainfodir}/dev.ibrahimaldhaheri.steelvoicemix.metainfo.xml
%{_datadir}/icons/hicolor/scalable/apps/steelvoicemix.svg

%changelog
* Tue Apr 21 2026 Ibrahim Aldhaheri <ibrahim@abokhalil.dev> - 0.2.0-1
- Initial SteelVoiceMix release (formerly nova-mixer).
- Rust daemon with a Unix-socket event stream for the optional Qt GUI.
- Virtual sinks via pactl null-sink + loopback so KDE Plasma's audio
  applet lists them as first-class output devices.
- GUI: tray icon, dial overlay (horizontal or vertical, configurable
  screen position), battery indicator, About dialog with disclaimer.
- systemd user services for the daemon and the GUI.
- OLED gauge disables itself gracefully on wireless firmware that
  rejects feature reports.
- Exponential-backoff reconnect when the base station is unplugged.
