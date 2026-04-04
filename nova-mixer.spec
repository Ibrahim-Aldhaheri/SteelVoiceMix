Name:           nova-mixer
Version:        0.1.0
Release:        1%{?dist}
Summary:        ChatMix for SteelSeries Arctis Nova Pro Wireless on Linux

License:        MIT
URL:            https://github.com/Ibrahim-Aldhaheri/Nova-mixer
Source0:        %{name}-%{version}.tar.gz

BuildArch:      noarch
BuildRequires:  python3-devel
BuildRequires:  systemd-rpm-macros

Requires:       python3
Requires:       python3-hidapi
Requires:       pipewire
Requires:       pulseaudio-utils
Requires:       libnotify

Recommends:     python3-pyside6

%description
Linux ChatMix implementation for the SteelSeries Arctis Nova Pro Wireless.
Creates virtual PipeWire sinks (NovaGame/NovaChat) controlled by the hardware
dial on the base station. Includes optional GUI monitor with battery indicator.

%prep
%autosetup

%install
# Main application
install -Dm755 nova-mixer.py %{buildroot}%{_bindir}/nova-mixer-daemon
install -Dm644 nova_mixer_core.py %{buildroot}%{_datadir}/%{name}/nova_mixer_core.py
install -Dm644 nova-mixer-gui.py %{buildroot}%{_datadir}/%{name}/nova_mixer_gui.py

# Launcher script
cat > %{buildroot}%{_bindir}/nova-mixer << 'EOF'
#!/bin/bash
PYTHONPATH=%{_datadir}/nova-mixer exec python3 %{_datadir}/nova-mixer/nova-mixer.py "$@"
EOF
chmod 755 %{buildroot}%{_bindir}/nova-mixer

# Wrapper for nova-mixer.py to find modules
cat > %{buildroot}%{_datadir}/%{name}/nova-mixer.py << 'PYEOF'
#!/usr/bin/python3
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nova_mixer_core import *

def main():
    import argparse
    parser = argparse.ArgumentParser(description="nova-mixer — ChatMix for Arctis Nova Pro Wireless")
    parser.add_argument("--gui", action="store_true", help="Launch with GUI monitor")
    parser.add_argument("--no-notify", action="store_true", help="Disable desktop notifications")
    args = parser.parse_args()
    if args.no_notify:
        global NOTIFY_ENABLED
        NOTIFY_ENABLED = False
    if args.gui:
        try:
            from nova_mixer_gui import main as gui_main
            gui_main()
        except ImportError:
            print("GUI requires PySide6: sudo dnf install python3-pyside6")
            sys.exit(1)
    else:
        mixer = NovaMixer()
        mixer.run()

if __name__ == "__main__":
    main()
PYEOF

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
%{_bindir}/nova-mixer-daemon
%{_datadir}/%{name}/
%{_userunitdir}/nova-mixer.service
%{_udevrulesdir}/50-nova-pro-wireless.rules
%{_datadir}/applications/nova-mixer.desktop
%{_metainfodir}/dev.ibrahimaldhaheri.nova-mixer.metainfo.xml

%changelog
* Sat Apr 04 2026 Ibrahim Aldhaheri <ibrahim@abokhalil.dev> - 0.1.0-1
- Initial release
- ChatMix dial support with PipeWire virtual sinks
- GUI monitor with battery indicator
- Auto-reconnect, desktop notifications
- udev auto-start, systemd service
