Name:           nova-mixer
Version:        0.2.0
Release:        1%{?dist}
Summary:        ChatMix for SteelSeries Arctis Nova Pro Wireless on Linux

License:        MIT
URL:            https://github.com/Ibrahim-Aldhaheri/Nova-mixer
Source0:        %{name}-%{version}.tar.gz

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
%autosetup

%build
cargo build --release

%install
# Daemon binary
install -Dm755 target/release/nova-mixer %{buildroot}%{_bindir}/nova-mixer

# GUI
install -Dm644 nova-mixer-gui.py %{buildroot}%{_datadir}/%{name}/nova-mixer-gui.py

# GUI launcher
cat > %{buildroot}%{_bindir}/nova-mixer-gui << 'EOF'
#!/bin/bash
exec python3 %{_datadir}/nova-mixer/nova-mixer-gui.py "$@"
EOF
chmod 755 %{buildroot}%{_bindir}/nova-mixer-gui

# Systemd user service
install -Dm644 nova-mixer.service %{buildroot}%{_userunitdir}/nova-mixer.service

# udev rules
install -Dm644 50-nova-pro-wireless.rules %{buildroot}%{_udevrulesdir}/50-nova-pro-wireless.rules

# Desktop file
install -Dm644 nova-mixer.desktop %{buildroot}%{_datadir}/applications/nova-mixer.desktop

# AppStream metadata
install -Dm644 dev.ibrahimaldhaheri.nova-mixer.metainfo.xml %{buildroot}%{_metainfodir}/dev.ibrahimaldhaheri.nova-mixer.metainfo.xml

%post
%systemd_user_post nova-mixer.service
udevadm control --reload-rules 2>/dev/null || :
udevadm trigger 2>/dev/null || :

%preun
%systemd_user_preun nova-mixer.service

%postun
udevadm control --reload-rules 2>/dev/null || :

%files
%license LICENSE
%doc README.md
%{_bindir}/nova-mixer
%{_bindir}/nova-mixer-gui
%{_datadir}/%{name}/
%{_userunitdir}/nova-mixer.service
%{_udevrulesdir}/50-nova-pro-wireless.rules
%{_datadir}/applications/nova-mixer.desktop
%{_metainfodir}/dev.ibrahimaldhaheri.nova-mixer.metainfo.xml

%changelog
* Mon Apr 06 2026 Ibrahim Aldhaheri <ibrahim@abokhalil.dev> - 0.2.0-1
- Rewrite daemon in Rust for performance and reliability
- GUI communicates with daemon over Unix socket
- Fix volume control bug (was using input.{sink} instead of sink name)
- Add battery polling to core daemon
- Consolidate duplicate Python code into single Rust binary
- Add exponential backoff for reconnection
