# Alpha-channel spec — used by the abokhalil/steelvoicemix-dev COPR
# project. Identical install layout to the stable spec; the only
# difference is how Version + Release are derived.
#
# Versioning. Stable spec hard-codes `Version: 0.3.1`. This dev spec
# uses rpkg's `git_dir_version` macro, which expands to something
# like `0.3.1.55.gba09e17` — the latest tag (`v0.3.1`) plus the
# commit count and short SHA since. RPM's vercmp orders these as
# stable 0.3.1 < dev 0.3.1.<n>.g<sha> < future stable 0.3.2,
# so users with both repos enabled always pull the highest version.
#
# Disable channel switching: just `dnf copr disable abokhalil/steelvoicemix-dev`
# and `dnf upgrade` (or `dnf downgrade steelvoicemix` if you're
# currently on a dev build that compares higher than the latest
# stable). Don't run both repos enabled long-term — alpha tracks
# break frequently.

Name:           steelvoicemix
Version:        {{{ git_dir_version }}}
Release:        1%{?dist}
Summary:        ChatMix for SteelSeries Arctis Nova Pro Wireless on Linux (alpha / dev channel)

License:        GPL-3.0-or-later
URL:            https://github.com/Ibrahim-Aldhaheri/SteelVoiceMix
Source0:        {{{ git_dir_pack }}}

BuildRequires:  rust >= 1.70
BuildRequires:  cargo
BuildRequires:  hidapi-devel
BuildRequires:  systemd-rpm-macros

Requires:       pipewire
Requires:       pulseaudio-utils
Requires:       pipewire-utils
Requires:       libnotify
Requires:       hidapi

Recommends:     python3-pyside6
# Same rationale as the stable spec — Microphone tab features need
# these to be present out of the box.
Requires:       noise-suppression-for-voice
Requires:       swh-plugins

%description
Linux ChatMix implementation for the SteelSeries Arctis Nova Pro Wireless.
ALPHA / development channel — built from the `dev` branch on every
commit. Updates frequently; expect rough edges. Use the stable
COPR project (abokhalil/steelvoicemix) if you want fewer surprises.

%prep
%autosetup -n {{{ git_dir_name }}}

%build
cargo build --release

%install
# Daemon binary
install -Dm755 target/release/steelvoicemix %{buildroot}%{_bindir}/steelvoicemix

# GUI entry shim + package
install -Dm644 steelvoicemix-gui.py %{buildroot}%{_datadir}/%{name}/steelvoicemix-gui.py
install -d %{buildroot}%{_datadir}/%{name}/gui
install -Dm644 gui/*.py %{buildroot}%{_datadir}/%{name}/gui/
install -d %{buildroot}%{_datadir}/%{name}/gui/tabs
install -Dm644 gui/tabs/*.py %{buildroot}%{_datadir}/%{name}/gui/tabs/

# Bundled ASM preset library + default HRIR
install -d %{buildroot}%{_datadir}/%{name}/gui/presets/asm/game
install -Dm644 gui/presets/asm/game/*.json \
    %{buildroot}%{_datadir}/%{name}/gui/presets/asm/game/
install -d %{buildroot}%{_datadir}/%{name}/gui/presets/asm/chat
install -Dm644 gui/presets/asm/chat/*.json \
    %{buildroot}%{_datadir}/%{name}/gui/presets/asm/chat/
install -d %{buildroot}%{_datadir}/%{name}/gui/data/hrir
install -Dm644 gui/data/hrir/EAC_Default.wav \
    %{buildroot}%{_datadir}/%{name}/gui/data/hrir/EAC_Default.wav

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

# App icon
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
# Changelog entries don't make sense in a per-commit dev build.
# rpkg auto-generates an entry from the latest commit; see the
# stable spec for human-curated history.
{{{ git_dir_changelog }}}
