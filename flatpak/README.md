# Flathub submission — GUI-only Flatpak

This directory holds the flatpak-builder manifest for the SteelVoiceMix
**GUI**. The Rust daemon stays on the host (COPR or source install). The
Flatpak reaches it through `$XDG_RUNTIME_DIR/steelvoicemix.sock`, which
finish-args bind-mount into the sandbox.

See the header comment in `dev.ibrahimaldhaheri.steelvoicemix.yml` for
why the daemon isn't bundled.

## Checklist before a Flathub PR

1. **Generate PySide6 pip sources.**

   Flathub's builders run offline — pip install has no network access, so
   every wheel needs to be listed as a source with a sha256. The
   `flatpak-pip-generator` script reads a requirements list and emits
   exactly that file.

   ```sh
   curl -LO https://raw.githubusercontent.com/flatpak/flatpak-builder-tools/master/pip/flatpak-pip-generator
   python3 flatpak-pip-generator \
       --output flatpak/pyside6-sources \
       PySide6-Essentials
   ```

   The output is `flatpak/pyside6-sources.json` (or `.yaml`) with every
   wheel + sha256 listed. Wire it into the manifest by replacing the
   placeholder `sources:` block under `python3-pyside6` with:

   ```yaml
       sources:
         - pyside6-sources.json
   ```

   Commit the generated file — Flathub's builders need it in the repo.

2. **Generate the 256×256 PNG icon.**

   Flathub's review wants both an SVG (we have it) and at least one
   raster PNG at 256×256. From the repo root:

   ```sh
   # rsvg-convert (librsvg2-bin / librsvg2-tools)
   mkdir -p data/icons/hicolor/256x256/apps
   rsvg-convert -w 256 -h 256 \
       data/icons/hicolor/scalable/apps/steelvoicemix.svg \
       -o data/icons/hicolor/256x256/apps/steelvoicemix.png

   # Fallback — ImageMagick 7
   magick data/icons/hicolor/scalable/apps/steelvoicemix.svg \
       -resize 256x256 \
       data/icons/hicolor/256x256/apps/steelvoicemix.png

   # Fallback — Inkscape
   inkscape --export-type=png --export-width=256 --export-height=256 \
       --export-filename=data/icons/hicolor/256x256/apps/steelvoicemix.png \
       data/icons/hicolor/scalable/apps/steelvoicemix.svg
   ```

   Commit the PNG.

3. **Test-build locally.**

   ```sh
   flatpak install flathub org.kde.Platform//6.7 org.kde.Sdk//6.7

   # Build + install into the user scope
   flatpak-builder --user --install --force-clean build-dir \
       flatpak/dev.ibrahimaldhaheri.steelvoicemix.yml

   # Run. Daemon must already be running on the host (from COPR or
   # install.sh --no-gui).
   flatpak run dev.ibrahimaldhaheri.steelvoicemix
   ```

   If the daemon is reachable, you'll see the same tray icon and window
   you get from a normal source install — just sandboxed.

4. **Validate the AppStream metadata.**

   ```sh
   flatpak install flathub org.freedesktop.appstream-glib
   flatpak run --command=appstream-util org.freedesktop.appstream-glib \
       validate dev.ibrahimaldhaheri.steelvoicemix.metainfo.xml
   ```

5. **Validate the manifest against Flathub's linter.**

   ```sh
   pipx install flatpak-builder-lint
   flatpak-builder-lint manifest flatpak/dev.ibrahimaldhaheri.steelvoicemix.yml
   ```

   Fix whatever it complains about before opening the PR — cuts one
   review cycle.

## Submitting to Flathub

Once the local build works and both validators are clean:

1. Fork <https://github.com/flathub/flathub>.
2. Create a new branch named `new-pr/dev.ibrahimaldhaheri.steelvoicemix`.
3. On the branch, create a directory `dev.ibrahimaldhaheri.steelvoicemix/`
   at the repo root and copy into it:
   - `dev.ibrahimaldhaheri.steelvoicemix.yml` (the manifest)
   - `pyside6-sources.json` (or `.yaml`, whichever flatpak-pip-generator
     produced)
   - A short `README.md` describing the app.
4. Open a PR against `flathub/flathub`. Flathub's review typically takes
   1–2 weeks. Common review comments you should expect:
   - **Trademark-adjacent naming.** If a reviewer flags "SteelSeries" or
     "Arctis Nova" in the summary/description, rephrase as "for
     SteelSeries headsets" rather than "SteelSeries headset software".
     The app ID itself is fine.
   - **Daemon not bundled.** Be ready to explain (repeat the
     justification in the manifest header) and offer to document the
     host-install step in the AppStream `<description>` tag.
   - **finish-args scope.** Any reviewer with security focus will
     question `--socket=fallback-x11`. Answer: Qt's window positioning
     under Wayland doesn't expose global coordinates, which breaks the
     overlay's corner-alignment; falling back to XWayland fixes that
     and is a pattern Flathub already accepts for Qt apps that paint
     free-standing overlays.

## Known limitations of the Flatpak'd GUI

- **Daemon must run on the host.** The Flatpak will not install or start
  the daemon. `sudo dnf copr enable abokhalil/steelvoicemix && sudo dnf
  install steelvoicemix` is the supported path; source-install with
  `./install.sh --no-gui` also works.
- **udev rule must be on the host.** Same reason — Flatpaks can't write
  into `/etc/udev/rules.d/`. The COPR package and `install.sh` both
  handle this.
- **Autostart lives on the host.** The `steelvoicemix-gui.service` user
  unit installed by the RPM launches the GUI from `/usr/bin/` — a
  Flatpak install doesn't replace that path. If both are installed the
  systemd unit wins and the Flatpak entry in the app menu becomes
  redundant. Pick one.
