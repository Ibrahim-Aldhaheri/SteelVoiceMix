# Contributing to SteelVoiceMix

SteelVoiceMix is a personal project — built for **Fedora KDE 44+** running on the **SteelSeries Arctis Nova Pro family** (Wireless and Wired). Contributions that fit that scope are welcome. Contributions that broaden the device coverage to other Arctis models, or to other distros, may be declined: ASM (Arctis Sound Manager) and EasyEffects already do those well, and stretching this codebase to match would dilute the things SteelVoiceMix does best.

## Quick links

- **Stable channel:** [`abokhalil/steelvoicemix`](https://copr.fedorainfracloud.org/coprs/abokhalil/steelvoicemix/) on COPR
- **Alpha channel:** [`abokhalil/steelvoicemix-dev`](https://copr.fedorainfracloud.org/coprs/abokhalil/steelvoicemix-dev/) — auto-builds on every dev-branch commit
- **Issue tracker:** [GitHub Issues](https://github.com/Ibrahim-Aldhaheri/SteelVoiceMix/issues)

## Branch model

- `main` — released code only. Moves forward by fast-forward / merge from `dev` at tag time.
- `dev` — daily development. **All PRs target `dev`, not `main`.**
- Feature branches → branch from `dev` → PR back into `dev`.
- The `main` default-branch on GitHub means casual cloners get stable code.

## Pull request process

1. **Open the PR against `dev`.** Don't target `main`.
2. **CI must be green** before review. The `.github/workflows/ci.yml` workflow runs on every PR:
   - `cargo build --release --locked` + `cargo test --locked --all-targets` + `cargo clippy --all-targets --no-deps --locked -- -D warnings`
   - `python3 -m py_compile` over every `.py` in the repo
   - XML validity check on `gui/translations/*.ts`
   - Cargo audit fires on Cargo.toml/Cargo.lock changes (separate workflow)
3. **Hardware test** if your PR touches `src/hid.rs`, `src/display.rs`, `src/mixer.rs`, or any HID-byte path. The maintainer's hardware is Wireless-only — Wired PRs need community verification.
4. **Keep commits focused.** One logical change per commit. Squash trivial fixups before opening.
5. **Don't tag releases or push to `main`.** That stays with the maintainer.

## Code style

### Rust

- `cargo fmt` before committing (CI doesn't enforce this yet, but matches the existing style).
- Clippy must pass under `-D warnings`. If you genuinely need to suppress a lint, do it locally with `#[allow(...)]` and a one-line WHY comment — never blanket-allow.
- Keep `unsafe` out of the daemon. None today.
- Tests live next to the code (`#[cfg(test)] mod tests`). 58 daemon tests run on every PR; add new ones for any new daemon command, opcode, or state-machine branch.

### Python (GUI)

- No runtime test suite (UI is hard to test without a display); CI just checks `py_compile`.
- Match the existing `gui/widgets.py` patterns — `card()`, `mode_picker()`, `bind_debounced_slider()`, `labelled_toggle()` — instead of building from scratch. Saves layout drift.
- Always wrap user-facing strings in `self.tr("...")`. The Arabic translation file picks them up; non-translated strings ship as English in every locale.
- Add `log.debug(...)` at decision points (state transitions, command boundaries, watchdog firings). The maintainer iterates against `STEELVOICEMIX_DEBUG=1` traces; missing logs make field bugs hard to diagnose.

## Hardware safety — the byte-exact rule

**Every byte that hits the device firmware via HID must come byte-exact from a verified upstream source** (ASM's per-device YAML files, LAM's HID command set, or — for new opcodes — a USB capture against the official SteelSeries GG software on Windows).

This is non-negotiable. The Nova Pro family has been bricked by experimental opcodes in other tools. We don't fuzz, we don't guess "what if we try `0xNN`", we don't ship anything you reverse-engineered yourself without a corroborating reference.

When porting a new opcode:

1. Cite the source in the commit message and in code comments (`// ASM nova_pro_wireless.yaml:189`).
2. Match the byte sequence exactly — no rounding, no "obvious" substitutions.
3. ASM's wired and wireless yamls differ in command padding, opcode set, and battery-reply layout; cross-reference the right one.

The full ruleset lives in `feedback_oled_bytes.md` in the maintainer's notes (paraphrased here).

## Bumping versions for the dev channel

When pushing to `dev` with code changes you want available on the `-dev` COPR:

```sh
scripts/bump-dev-version.sh
```

This bumps the betaN suffix in `steelvoicemix-dev.spec`, `Cargo.toml`, and `gui/settings.py` in lockstep. Without it, the `Dev Beta Release` workflow runs but skips (no version change → no tag → no COPR build).

For pure docs / CI / non-code commits to `dev`, skip the bump.

## Adding a new language

We use Qt Linguist `.ts` files compiled to `.qm` at build time. Adding a new language is simple but only worth doing if **you can keep it maintained** — half-translated UIs that drift behind English are worse than English-only.

To add `<locale>` (e.g. `de`, `fr`, `pt_BR`, `es`):

1. Copy `gui/translations/steelvoicemix_ar.ts` to `gui/translations/steelvoicemix_<locale>.ts`.
2. Update the header: `<TS version="2.1" language="<locale>" sourcelanguage="en">`.
3. Replace each `<translation>...</translation>` body with your locale, OR mark it `<translation type="unfinished"></translation>` to skip for now.
4. **Read the `<extracomment>` lines** — they're translator-facing context for ambiguous strings (e.g. "Section title above the OLED brightness slider on the Deck tab"). They're there to help you pick the right register.
5. Validate XML before opening the PR:

   ```sh
   python3 -c "import xml.etree.ElementTree as ET; ET.parse('gui/translations/steelvoicemix_<locale>.ts')"
   ```

6. The build automatically picks up the new `.ts`; no spec-file or code edit needed.

Languages with the highest expected reach for the Linux/Fedora/SteelSeries audience: German (`de`), French (`fr`), Spanish (`es`), Brazilian Portuguese (`pt_BR`), Russian (`ru`).

## Reporting a bug

Please include:

1. **Hardware variant**: Nova Pro Wireless USB / Wireless Xbox / Wired USB / Wired Xbox.
2. **Distro + DE**: Fedora 44 KDE / GNOME / etc.
3. **Daemon log** with debug enabled. Edit `/etc/systemd/user/steelvoicemix.service.d/override.conf` to set `Environment=RUST_LOG=debug`, restart with `systemctl --user restart steelvoicemix`, reproduce, then attach:

   ```sh
   journalctl --user -u steelvoicemix -n 200 --no-pager
   ```

4. **Settings dump**: `cat ~/.config/steelvoicemix/daemon.json` and `cat ~/.config/steelvoicemix/settings.json`.
5. **Reproduction steps** — what you clicked, what you expected, what you got.

The maintainer can `ssh` into a Fedora KDE test machine for live debugging on PRs that need it.

## Project conventions worth knowing

- **No `Co-Authored-By: Claude` trailer** in commits, even if you used Claude for help. Author yourself or a real human.
- **Commit message style**: imperative subject, max ~70 chars. Body is optional but encouraged for non-obvious changes — explain WHY, not WHAT (the diff shows WHAT). Wrap at ~72 chars.
- **No tag/release without explicit maintainer ask.** PRs stop at "merged into dev"; the maintainer cuts releases.
- **No new dependencies** without a strong reason. Every dep is a future audit + a future suspend-recovery edge case.
- **Memory of past architectural decisions** lives in the maintainer's notes, not in the repo. If something seems weird, open an issue and ask before refactoring — there's usually a "we tried that, it broke X" story behind it.

## Credit and lineage

SteelVoiceMix stands on three upstream projects (full credit in the [README](README.md)):

- **nova-chatmix-linux** — reverse-engineered the HID protocol.
- **Linux-Arctis-Manager** — original open-source Linux Arctis manager.
- **Arctis Sound Manager** — multi-channel mixer + EQ + HeSuVi spatial audio pioneer.

If your contribution ports a feature from one of these, cite the source file in the commit message. We benefit from their work; we credit them in return.

## Questions

Not sure if your idea fits the project's scope? Open an issue tagged `discussion` before writing code. Better to spend 5 minutes aligning than 5 hours building something the maintainer would decline.
