Name:           steelvoicemix
Version:        0.3.1
Release:        1%{?dist}
Summary:        ChatMix for SteelSeries Arctis Nova Pro Wireless on Linux

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
# Microphone-tab features depend on these LADSPA plugins. Required
# (not just recommended) so the Microphone tab works out of the box
# — combined size is ~6 MB, small enough that always-installing them
# beats the friction of "why doesn't AI NC work?" for users who
# don't read the README. Daemon still spawns the chain lazily; if a
# user genuinely wants to drop the deps, they can `dnf remove
# --noautoremove`.
# Noise Gate uses gate_1410 from ladspa-swh-plugins — that package
# is in the main Fedora repos so we hard-Require it.
Requires:       ladspa-swh-plugins
# AI Noise Cancellation + Noise Reduction need librnnoise_ladspa.so
# from werman/noise-suppression-for-voice. That LADSPA wrapper
# isn't packaged in Fedora's main repos, so we Recommend rather
# than Require — dnf install proceeds cleanly even when it's
# missing, and the GUI's startup probe disables the relevant
# toggles with a hint pointing to where to get the plugin.
Recommends:     noise-suppression-for-voice

%description
Linux ChatMix implementation for the SteelSeries Arctis Nova Pro Wireless.
Rust daemon that creates virtual PipeWire sinks (SteelGame/SteelChat) controlled
by the hardware dial on the base station. Includes optional PySide6 GUI monitor
with battery indicator that communicates with the daemon over a Unix socket.

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

# Bundled ASM preset library (~400 game/chat tunings) — read-only,
# refreshed by the maintainer via scripts/fetch_asm_presets.py.
install -d %{buildroot}%{_datadir}/%{name}/gui/presets/asm/game
install -Dm644 gui/presets/asm/game/*.json \
    %{buildroot}%{_datadir}/%{name}/gui/presets/asm/game/
install -d %{buildroot}%{_datadir}/%{name}/gui/presets/asm/chat
install -Dm644 gui/presets/asm/chat/*.json \
    %{buildroot}%{_datadir}/%{name}/gui/presets/asm/chat/

# Bundled default HRIR (HeSuVi-format 14-channel WAV).
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
* Thu Apr 30 2026 Ibrahim Aldhaheri <ibrahim@abokhalil.dev> - 0.3.1-1
- Fix: 'Check for updates' button now wipes the on-disk update cache
  before querying GitHub, so it actually re-checks instead of
  replaying the last 24 h's cached result.
- Fix: EQ slider labels fall back to 'Band 1..10' for parametric
  presets where multiple bands cluster in the same musical range
  (the ASM library does this for game-specific tunings — three
  sliders all labelled 'Brilliance' was confusing).
- UX: ALPHA badge added next to the HDMI sink toggle to signal it
  hasn't been hardware-verified against a real TV / AVR yet.

* Thu Apr 30 2026 Ibrahim Aldhaheri <ibrahim@abokhalil.dev> - 0.3.0-1
- Major feature: 10-band parametric EQ. Per-channel (Game / Chat /
  Media / HDMI) tunings driven by PipeWire filter chains, with a
  searchable preset library. 391 ASM-curated game/chat presets are
  bundled in the package; users can save / rename / delete their own
  via the EQ tab. Up to 5 favourites per channel pinned to a quick-
  access bar above the sliders. Editing a built-in preset auto-forks
  to a Custom-N user preset so originals stay clean.
- Major feature: virtual surround over headphones. New SteelSurround
  7.1 sink + HRIR convolver chain (HeSuVi-format reference HRIR
  bundled). Surround is on by default; every headphone-path channel
  (Game / Chat / Media) routes through the convolver before reaching
  the headset. HDMI bypasses the HRIR chain since the downstream
  device handles surround natively.
- GUI overhaul: left sidebar nav replaces the top-tab strip; each
  tab uses card-style sections; animated ToggleSwitch replaces the
  plain QCheckBox where used as on/off; status pill in the header
  goes green/red on connection state. Window grew to 880×720 with
  scrollable tab pages.
- New tabs: Equalizer, Surround, Microphone (placeholder for noise
  gate / NR / AI noise cancellation — UI only this release).
- New: settings reset-to-defaults button (preserves saved audio
  profiles), test-audio clips synthesised at runtime (pink noise,
  white noise, sweeps, tones) for ear-checking the EQ chain at
  conservative reference levels with 200 ms fade-in.
- New: SearchableSelect widget — JS-style dropdown with integrated
  search field, keyboard nav, and ignored mouse-wheel events.
- Fix: surround channel mapping (HeSuVi 14-channel WAV layout).
  Previous rev had FR_L/FR_R reversed past channel 6, causing right-
  channel content to bleed into the left ear.
- Fix: dial overlay no longer shows in the KDE Plasma 6 taskbar.
- Fix: minimize-to-tray toast is now opt-in (disabled by default).
- Tooling: scripts/fetch_asm_presets.py refreshes the bundled preset
  set from upstream on demand.
- Trademark hygiene: scrubbed all references to 'Sonar' from the
  application surface (kept descriptive nominative use in README
  context only).

* Fri Apr 24 2026 Ibrahim Aldhaheri <ibrahim@abokhalil.dev> - 0.2.4-1
- Fix: the dial overlay now always appears on the primary monitor.
  Previously it followed the mouse cursor, which put the gauge on
  whichever screen happened to be "active" — confusing on multi-
  display setups where the headset and the mouse weren't on the
  same screen.
- Release pipeline: spec uses rpkg {{{ git_dir_pack }}} for SRPM
  generation, removing the external tarball URL dependency.
- CI: GitHub Actions workflow replaces the COPR webhook for release
  triggers. Auto-merge lands Dependabot patch/minor bumps on green CI.
- Deps: actions/checkout v6, actions/setup-python v6, libc 0.2.185,
  thiserror 2. Dropped unused signal-hook dependency.

* Fri Apr 24 2026 Ibrahim Aldhaheri <ibrahim@abokhalil.dev> - 0.2.3-1
- Virtual sinks renamed to Steel* (SteelGame, SteelChat). Legacy Nova*
  orphans from earlier installs are swept on startup.
- Add SteelMedia: a third virtual sink that bypasses the ChatMix dial
  for apps (music, browsers) that shouldn't duck during voice. Toggle
  from the GUI "Add Media" / "Remove Media" button; preference persists
  across restarts.
- Fix: "Add Media" / "Remove Media" buttons were silently broken. The
  GUI sent commands on the subscribe socket, but the daemon stops
  reading from that socket after subscribe. Commands now go over a
  fresh short-lived connection per click.
- Sink descriptions drop the hyphen — SteelGame/SteelChat/SteelMedia
  now match their sink names in Plasma's audio applet.
- install.sh --no-gui flag for headless / Sway / tray-less setups.
- Dev infra: Dependabot config + weekly cargo-audit cron.

* Wed Apr 22 2026 Ibrahim Aldhaheri <ibrahim@abokhalil.dev> - 0.2.2-1
- Fix systemd user units under RPM install. The 0.2.1 service files
  used ExecStart=%%h/.local/bin/steelvoicemix, which resolves to a
  per-user path that the RPM never populates — the service would
  restart-loop with status=203/EXEC. Switched the master units to
  ExecStart=/usr/bin/steelvoicemix; install.sh rewrites the path
  back to %%h/.local/bin/ for source installs.

* Wed Apr 22 2026 Ibrahim Aldhaheri <ibrahim@abokhalil.dev> - 0.2.1-1
- Ship app icon (data/icons/hicolor/scalable/apps/steelvoicemix.svg)
  and README screenshots — v0.2.0 tag pre-dated those commits and
  its COPR build failed on the missing icon during %install.
- Flag the Summary as "(beta)" and add type="development" to the
  AppStream release entry so dnf / GNOME Software / Discover show
  the pre-release status.

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
