# Alpha channel — setup notes

A second COPR project, `abokhalil/steelvoicemix-dev`, builds on every
push to the `dev` branch. Users opt in by switching repos; everyone
else stays on the stable `abokhalil/steelvoicemix` channel.

This doc covers the one-time setup the maintainer runs to make the
alpha channel exist. End-user instructions for switching are in the
GUI's Settings → Alpha Channel card (which copies the dnf commands
to the clipboard).

## Maintainer setup (one-time)

1. **Create the COPR project**

   At <https://copr.fedorainfracloud.org/coprs/> click "New Project":

   - **Project name:** `steelvoicemix-dev`
   - **Description:** "SteelVoiceMix alpha builds — rebuilt from the
     `dev` branch on every commit. Bleeding-edge; expect rough
     edges."
   - **Chroots:** `fedora-43-x86_64`, `fedora-rawhide-x86_64`
     (mirror what stable ships).
   - **Build options → Networking:** check "Enable internet access
     during build" (cargo needs network for crate downloads).

2. **Add the package source**

   In the new project: **Packages → Add new package** with these
   fields:

   - **Source type:** Custom
   - **Script:**
     ```bash
     #!/bin/bash
     set -eu
     dnf -y install rpkg-util git
     git clone --depth=50 https://github.com/Ibrahim-Aldhaheri/SteelVoiceMix.git
     cd SteelVoiceMix
     git checkout dev
     rpkg srpm --spec steelvoicemix-dev.spec --outdir "$outdir"
     ```
   - **Auto-rebuild:** off (GitHub Actions triggers builds explicitly).

3. **Confirm the COPR API token is in GitHub Secrets**

   The `COPR_CONFIG` secret already exists for the stable workflow;
   the dev workflow reuses it. If you've rotated tokens, regenerate
   at <https://copr.fedorainfracloud.org/api> and update the secret.

That's it. Every `git push origin dev` then triggers
`.github/workflows/copr-dev.yml`, which calls
`copr-cli build-package abokhalil/steelvoicemix-dev`, which runs the
custom script above against the latest `dev`.

## End-user flow

The GUI's Settings → Alpha Channel card has two "📋 Copy" buttons:

- **Switch to alpha:** copies
  ```
  sudo dnf copr enable abokhalil/steelvoicemix-dev -y && \
  sudo dnf upgrade steelvoicemix --refresh -y
  ```
- **Back to stable:** copies
  ```
  sudo dnf copr disable abokhalil/steelvoicemix-dev -y && \
  sudo dnf copr enable abokhalil/steelvoicemix -y && \
  sudo dnf distro-sync steelvoicemix -y
  ```

Users paste into a terminal. The GUI can't actually elevate to sudo,
so this is the honest UX rather than fake automation.

## Versioning

Stable spec: hard-coded `Version: 0.3.1` (or whatever the current
release is).

Dev spec (`steelvoicemix-dev.spec`): `Version: {{{ git_dir_version }}}`,
which expands to e.g. `0.3.1.55.gba09e17` (latest tag + commit count
since + short SHA). RPM's vercmp orders these as

```
0.3.1 < 0.3.1.55.gba09e17 < 0.3.2
```

so users with both repos enabled always get the highest version, and
switching back to stable downgrades cleanly via `dnf distro-sync`.
