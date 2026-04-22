# Flathub submission — work in progress

This directory holds the flatpak-builder manifest and support files for an
eventual Flathub submission. **The manifest does not build yet**; see the
TODO comments in `dev.ibrahimaldhaheri.steelvoicemix.yml` for what's
outstanding.

## Outstanding work

1. **Generate `cargo-sources.json`** from the current `Cargo.lock` so the
   Rust build runs offline. Flathub's builders have no network.

   ```sh
   curl -LO https://github.com/flatpak/flatpak-builder-tools/raw/master/cargo/flatpak-cargo-generator.py
   python3 flatpak-cargo-generator.py -o flatpak/cargo-sources.json ../Cargo.lock
   ```

   Then reference it from the Rust module's `sources:` block.

2. **Generate PySide6 pip sources** the same way:

   ```sh
   curl -LO https://github.com/flatpak/flatpak-builder-tools/raw/master/pip/flatpak-pip-generator
   python3 flatpak-pip-generator --requirements-file <(echo "PySide6==6.7.0") \
       --output flatpak/pyside6-sources
   ```

3. **Raster app icon** at 256×256 PNG. Flathub's review wants both the SVG
   (which we already have) and at least one raster size. Generate with:

   ```sh
   rsvg-convert -w 256 -h 256 \
       data/icons/hicolor/scalable/apps/steelvoicemix.svg \
       -o data/icons/hicolor/256x256/apps/steelvoicemix.png
   ```

4. **Decide daemon placement.** Options:
   - **Bundle the daemon inside the Flatpak** — requires
     `--device=all` in finish-args to access the hidraw node, which
     Flathub scrutinises. User still needs the udev rule installed on
     the host (Flatpaks can't install udev rules).
   - **Require the host daemon** (installed via COPR or `install.sh`) —
     Flatpak ships GUI only and talks to the daemon via
     `xdg-run/steelvoicemix.sock`. Simpler sandbox, but fragments the
     install story.

   The manifest currently assumes the second option.

5. **Screenshots URL check.** AppStream screenshot URLs must resolve
   from the public internet. Our `raw.githubusercontent.com/...` paths
   already do, so no change needed.

6. **Test build locally** before submitting:

   ```sh
   flatpak install flathub org.kde.Platform//6.7 org.kde.Sdk//6.7 \
       org.freedesktop.Sdk.Extension.rust-stable//24.08
   flatpak-builder --user --install --force-clean build-dir \
       flatpak/dev.ibrahimaldhaheri.steelvoicemix.yml
   flatpak run dev.ibrahimaldhaheri.steelvoicemix
   ```

## Submitting to Flathub

When the local build works:

1. Fork `flathub/flathub` on GitHub.
2. Create a new branch named after the app ID:
   `new-pr/dev.ibrahimaldhaheri.steelvoicemix`
3. Copy this `flatpak/` directory into the fork as
   `dev.ibrahimaldhaheri.steelvoicemix/` (one level deep).
4. Open a PR against `flathub/flathub`. The review cycle typically
   takes a week or two and covers sandbox permissions, metadata
   correctness, and trademark-adjacent naming. Be ready to rename if
   Flathub reviewers flag "Nova" or "SteelSeries" references.
