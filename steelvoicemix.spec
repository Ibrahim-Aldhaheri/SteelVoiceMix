Name:           steelvoicemix
Version:        0.4.0
Release:        1%{?dist}
Summary:        ChatMix for SteelSeries Arctis Nova Pro Wireless on Linux

License:        GPL-3.0-or-later
URL:            https://github.com/Ibrahim-Aldhaheri/SteelVoiceMix
Source0:        {{{ git_dir_pack }}}

BuildRequires:  rust >= 1.70
BuildRequires:  cargo
BuildRequires:  hidapi-devel
BuildRequires:  systemd-rpm-macros
BuildRequires:  qt6-linguist

Requires:       pipewire
Requires:       pulseaudio-utils
Requires:       pipewire-utils
# wmctrl powers the Auto Game-EQ Add Binding dialog's 'open
# windowed apps' suggestion list. Recommends so dnf doesn't break
# the install when wmctrl isn't available (e.g. transient repo
# issues, derivative distros). Existing users upgrading from
# earlier versions can `dnf install wmctrl` manually if they
# want the window-suggestion source.
Recommends:     wmctrl
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
# ladspa-swh-plugins is in main Fedora repos so we hard-Require it
# (Noise Gate). librnnoise_ladspa.so for the AI/NR features comes
# from werman/noise-suppression-for-voice which isn't in any RPM
# repo — Recommend so install doesn't fail; the GUI's LADSPA
# probe disables those toggles when missing and points users at
# the github project for a manual build.
Requires:       ladspa-swh-plugins
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
# Compile bundled translations.
for ts in gui/translations/*.ts; do
    lrelease-qt6 "$ts" -qm "${ts%.ts}.qm"
done

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
install -d %{buildroot}%{_datadir}/%{name}/gui/presets/asm/mic
install -Dm644 gui/presets/asm/mic/*.json \
    %{buildroot}%{_datadir}/%{name}/gui/presets/asm/mic/

# Bundled default HRIR (HeSuVi-format 14-channel WAV).
install -d %{buildroot}%{_datadir}/%{name}/gui/data/hrir
install -Dm644 gui/data/hrir/EAC_Default.wav \
    %{buildroot}%{_datadir}/%{name}/gui/data/hrir/EAC_Default.wav

# Translations — compiled .qm files only; .ts sources stay in-tree
# for contributors but don't ship.
install -d %{buildroot}%{_datadir}/%{name}/gui/translations
for qm in gui/translations/*.qm; do
    [ -e "$qm" ] || continue
    install -Dm644 "$qm" \
        %{buildroot}%{_datadir}/%{name}/gui/translations/$(basename "$qm")
done

# CLI wrapper — exposes `steelvoicemix-cli sink cycle` for users
# who want to bind a global keyboard shortcut via their DE.
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
%{_bindir}/steelvoicemix-cli
%{_datadir}/%{name}/
%{_userunitdir}/steelvoicemix.service
%{_userunitdir}/steelvoicemix-gui.service
%{_udevrulesdir}/50-nova-pro-wireless.rules
%{_datadir}/applications/steelvoicemix.desktop
%{_metainfodir}/dev.ibrahimaldhaheri.steelvoicemix.metainfo.xml
%{_datadir}/icons/hicolor/scalable/apps/steelvoicemix.svg

%changelog
* Sat May 03 2026 Ibrahim Aldhaheri <ibrahim@abokhalil.dev> - 0.4.0-1
- New: Volume Boost — per-channel 100-200% digital amplification on
  Game / Chat / Media / HDMI sinks. Toggle + slider on the Sinks tab,
  with a clipping-risk warning above 150%. Snapshots the chatmix dial
  position when boost engages and restores it when boost disables, so
  lowering the dial to compensate for boost no longer strands you at
  the now-too-low level.
- New: Multi-language UI. Settings → Appearance language picker
  (System default / English / Arabic). Arabic ships with right-to-
  left layout. Translation coverage is partial — strings without a
  translation fall back to English. Language switch prompts to
  restart the GUI for full effect; the daemon stays up.
- New: Default-sink cycle. Optional keyboard shortcut (default
  Ctrl+Shift+S) cycles the system default between SteelGame /
  SteelChat / SteelMedia / SteelHDMI; per-sink exclude in Settings.
  Standalone steelvoicemix-cli `sink cycle` exposes the same path
  for global window-manager bindings.
- Update checker is channel-aware: dev users see dev releases,
  stable users see stable. About dialog's APP_VERSION derives from
  RPM at runtime so it always matches what's installed.
- Audio glitch fixes: filter chains now set node.lock-quantum,
  node.suspend-on-idle = false, node.latency = 1024/48000, and the
  surround convolver pins blocksize = 512. Defends against PipeWire
  bug #4013 / EasyEffects #1567 false-resync glitches during fast
  scene transitions and alt-tab refocus.
- Chatmix dial position is now persisted across daemon restarts.
  No more 100/100 reset when systemctl restart misses the firmware
  query window.
- Auto Game-EQ snapshot is persisted to disk. Closing a game and
  suspending immediately (before the watcher's exit grace fires)
  no longer leaks the game preset into the user's "default" EQ.
- Bug fixes: English language pick falling back to system locale
  (loaded Arabic on Arabic systems); Volume Boost slider lag (now
  debounced); preset combo flipping to wrong preset on test-audio
  playback; preset/bands mismatch on restart; wmctrl is Recommends
  not Requires (avoids broken installs on edge-case repo states).

* Fri May 01 2026 Ibrahim Aldhaheri <ibrahim@abokhalil.dev> - 0.3.2-1
- Major feature: Microphone capture-side processing. Three
  independent toggles (Noise Gate / Noise Reduction / AI Noise
  Cancellation), each with a strength slider. Daemon spawns one
  PipeWire filter chain covering the enabled combination. New
  Volume Stabilizer adds compression — Broadcast (SC4 mono) or
  Soft (Dyson) modes. Microphone is exposed as the SteelMic
  virtual source apps record from. All mic features tagged ALPHA.
- Major feature: per-channel mic EQ — the mic chain now runs 10
  biquad bands after the gate / NR / AI-NC stages. Microphone
  appears as a fifth channel in the Equalizer tab alongside Game /
  Chat / Media / HDMI. Bundled 14 ASM mic presets (Balanced,
  Walkie Talkie, Less Nasal, Broadcast, etc.) plus a built-in Flat.
- Major feature: Auto Game-EQ. Background watcher polls
  PipeWire's sink-inputs and applies a matching ASM preset on the
  Game channel when a known game appears. Drag-orderable manual
  bindings let users override the auto-match. Snapshot/restore
  flow preserves the user's pre-game EQ. Friendlier Add Binding
  dialog now combines active audio clients, open windows
  (via wmctrl), and free-text input.
- Audio profiles now snapshot the live EQ + mic state in addition
  to overlay options + Media / HDMI sink toggles. One-click
  load restores everything via daemon commands.
- Voice test ('Hear yourself') loopback with a shared service so
  the Microphone tab and the EQ tab's Mic channel drive the same
  pw-loopback subprocess.
- Theme switcher: Auto / Light / Dark. Window now responsive
  (minimum 820x660, default 900x740, no longer fixed-size).
- Sidetone reverted to a 4-step slider (Off / Low / Medium /
  High) tagged ALPHA — the wireless variant's firmware may
  ignore HID writes despite the slider quantising correctly.
- Home tab redesigned: 2-column ChatMix + Headset cards plus a
  full-width 'Active Features' pill row showing what processing
  is currently on at a glance.
- Settings: Report Issue button (copies a diagnostic block to the
  clipboard + opens GitHub's New Issue page). Start-minimised-to-
  tray toggle. Alpha-channel switch buttons.
- Mic chain watchdog: detects dead pw-loopback subprocesses after
  system suspend and respawns them automatically.
- Mouse-wheel scroll on EQ + Mic sliders now ignored — accidental
  scrolls were forking the active preset to a fresh Custom-N.
- Logging: per-tick chatter (game watcher, reconcile, default-
  source promotions) demoted to DEBUG. Set RUST_LOG=debug or
  STEELVOICEMIX_DEBUG=1 to see the full trail.
- Crash fix: Qt aborted with 'QThread destroyed while running'
  on every systemd service restart — cleanup now routed through
  QApplication::aboutToQuit so every exit path stops the threads.

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
