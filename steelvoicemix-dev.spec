# Beta-channel spec — used by the abokhalil/steelvoicemix-dev COPR
# project. Installs the same files as the stable spec under the
# same package name (`steelvoicemix`); users pick a channel by
# which COPR repo is enabled, following the conventional Fedora /
# COPR pattern (same model the kernel uses across stable/testing/
# rawhide).
#
# Channel switching:
#
#   # Move from stable to dev:
#   sudo dnf copr enable abokhalil/steelvoicemix-dev
#   sudo dnf upgrade steelvoicemix --refresh
#
#   # Back to stable:
#   sudo dnf copr disable abokhalil/steelvoicemix-dev
#   sudo dnf distro-sync steelvoicemix
#
# Don't keep both repos enabled at once.
#
# Versioning convention. Both specs hard-code Version manually;
# auto-versioning via rpkg's `git_dir_version` macro caused weird
# fallback strings (0.0.git.<count>.<sha>) under COPR's depth-
# limited clone. Manual is simpler and more predictable.
#
# This spec carries the BETA of the next stable release. Bump the
# beta number when cutting a new dev snapshot for users to test:
#
#   stable               0.3.1   (steelvoicemix.spec)
#   dev / beta           0.3.2~beta1
#                        0.3.2~beta2
#                        0.3.2~beta3
#   next stable          0.3.2   (when ready: bump steelvoicemix.spec
#                                 to 0.3.2 and dev spec to 0.3.3~beta1)
#
# RPM's vercmp orders `0.3.2~betaN < 0.3.2`, so users on the dev
# repo who switch back to stable upgrade cleanly to the next
# stable release.

Name:           steelvoicemix
Version:        0.4.1~beta6
Release:        1%{?dist}
Summary:        ChatMix for SteelSeries Arctis Nova Pro Wireless on Linux (beta / dev channel)

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
# wmctrl powers the Auto Game-EQ Add Binding dialog's 'open
# windowed apps' suggestion list — pulled pre-emptively so the
# binding flow lights up out of the box on X11 sessions.
Requires:       wmctrl
Requires:       libnotify
Requires:       hidapi

Recommends:     python3-pyside6
# ladspa-swh-plugins is in main Fedora repos. librnnoise_ladspa.so
# for AI/NR isn't in any RPM repo — Recommend so install doesn't
# fail; GUI probe disables those toggles when missing.
Requires:       ladspa-swh-plugins
Recommends:     noise-suppression-for-voice

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
install -d %{buildroot}%{_datadir}/%{name}/gui/presets/asm/mic
install -Dm644 gui/presets/asm/mic/*.json \
    %{buildroot}%{_datadir}/%{name}/gui/presets/asm/mic/
install -d %{buildroot}%{_datadir}/%{name}/gui/data/hrir
install -Dm644 gui/data/hrir/EAC_Default.wav \
    %{buildroot}%{_datadir}/%{name}/gui/data/hrir/EAC_Default.wav

# CLI wrapper — `steelvoicemix-cli sink cycle` exposed for global
# keyboard-shortcut binding via the user's DE settings.
install -Dm755 steelvoicemix-cli.py %{buildroot}%{_bindir}/steelvoicemix-cli

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
%{_bindir}/steelvoicemix-cli
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
