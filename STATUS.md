# SteelVoiceMix — status: real vs. hardware-dependent

This file tracks which behaviour is verified on this headless dev box
(no display, no headset) versus what still needs checking on the Fedora
KDE target with real hardware.

## Verification environment

- Dev box: Ubuntu server, no GUI, no headset. GUI is exercised via an
  **offscreen Qt** harness + pytest suite (`tests/`, run with
  `QT_QPA_PLATFORM=offscreen`). The daemon client is stubbed, so tests
  cover layout, wiring, and command emission — not real audio/DSP.
- Target: Fedora KDE (Breeze icons, real PipeWire, real Arctis Nova
  Pro). Anything touching the device or the audio graph is **not**
  verified here.

## UI/UX redesign pass — DONE and verified offscreen

| Area | Change | Verified how |
|---|---|---|
| Home | Rebuilt as a dashboard; resolved the header-vs-body "connected" contradiction; battery-before-presence ordering bug fixed | render + interaction tests |
| Equalizer | Three sparse cards → one compact toolbar; bands above the fold; delete confirm; emoji-free channel combo | render + interaction tests |
| Surround | Plain-language rewrite; jargon behind "?"; profile→enable flow | render + interaction tests |
| Sinks/Deck | Responsive fix (no more clipped controls at min width); emoji cleanup | render at 820/1180/1500 |
| Settings | Two-column reflow; destructive actions confirm | render |
| Shell | Real theme icons (Breeze on target); pill-style nav selection; slim scrollbars | render |
| Tests | 21 offscreen GUI + logic tests, wired into CI ("Python + GUI tests") | pytest |

### Needs on-hardware confirmation (Fedora KDE)
- Sidebar/nav icons resolve to real Breeze icons (dev box has no icon
  theme, so they fall back to a drawn dot here).
- Real light/dark system palette via the XDG portal.
- Actual battery/ChatMix/device events populating the Home hero.

## Surround stage editor — IN PROGRESS

Goal: an interactive stage where the listener is centred and each active
channel is shown in its layout position, controlling **real** DSP — not
a decorative diagram.

### Backend truth (confirmed by reading src/surround_chain.rs + audio.rs)

The surround chain is **fixed HRIR convolution** (`src/surround_chain.rs`):
each of the 8 input channels (FL FR FC LFE RL RR SL SR) is convolved
with a specific slice of a HeSuVi 14-channel WAV and summed to binaural
stereo. Consequences, and what the editor may therefore honestly do:

| Requested control | Physically supported here? | Editor behaviour |
|---|---|---|
| Direction (azimuth: front/back, left/right) | **No** — baked into the HRIR impulse per channel. A different WAV is the only way to change it. | Speakers shown at their **fixed** standard positions; azimuth is **not** draggable. Documented in-UI. |
| Elevation (height) | **No** — the HeSuVi layout is planar; no height channels. | Not offered. |
| Per-channel **level** (loudness) | **Yes** — a gain node baked into the chain config. | Radial drag = level (toward centre = louder, away = quieter). This is loudness, **not** physical distance modelling — labelled as level. |
| **Mute / Solo** | **Yes** — zero the channel's gain / all others. | Per-speaker buttons. |
| **Test tone** per channel | **Yes** — the daemon can play a tone into one channel. | Per-speaker test button. |
| Per-channel **delay** (distance/time-align) | Technically possible (a `delay` node) but on a binaural HRIR chain it fights the ITD already encoded in the impulse and smears localisation. | **Not** offered — would degrade, not help. Documented. |

**How gains reach the audio graph:** the same way EQ band gains already
do — the config is regenerated and the filter-chain child is respawned
(`respawn_channel_chain` is the shipped pattern for EQ). The GUI commits
on drag-**release** (not continuously), so there is one respawn per
adjustment, matching the EQ tab. A brief (~0.5–1 s) audio glitch per
level change is expected, same as changing an EQ band.

### Specialist review outcomes (DSP + interaction)

Two specialists reviewed the first cut. Resolved from their findings:
- **DSP-critical:** the `linear` gain nodes used `config = { mult }`,
  which that builtin ignores — every channel would have run at unity
  gain (the whole editor a silent no-op). Fixed to the control-port
  form `control = { "Mult" = … "Add" = 0.0 }`, with a regression test
  asserting the rendered conf.
- **Honesty/state sync:** the saved profile is now replayed to the
  daemon on connect and on device switch (it previously started flat
  while the UI showed trims); preset/reset/undo push a single batched
  `set-surround-gains` (one respawn, and it always sends mute state so
  un-mute reaches the daemon); reset/undo clear daemon solo.
- **Robustness:** the test tone is refused unless the chain is running
  (so it can't leak a beep to the default sink), and its `pw-play`
  child is reaped (no zombies).
- **Interaction:** keyboard selection now actually works (Left/Right
  around the ring, real Tab left to focus); M/S/T shortcuts implemented;
  undo snapshots before a drag (was a no-op for drags); keyboard commit
  debounced; selection ring uses the accent colour not the warning
  colour; muted vs soloed-away are visually distinct and a "Solo: …"
  status line shows when solo is active; an off-state notice appears
  when surround is off; nearest-marker hit-testing.

### Hardware finding: mute didn't attenuate → switched to convolver gain

On the first hardware test, muting a channel didn't silence it. Dumping
the generated config showed it was structurally correct (muted channel
→ `linear` node with `"Mult" = 0.00000`, valid per the PipeWire docs
and matching the working EQ chain's `control = {}` form). But the
`linear` node's Mult control did not attenuate in practice.

Rather than keep debugging the control-port path, the level trim now
uses the **convolver's own `gain` config parameter** — a documented,
config-time value applied when the impulse response loads, with no
control-port mechanism to misfire. Each channel's two ear convolvers
share the channel's linear gain (0.0 when muted/soloed away), so the
HRIR's L/R balance is preserved. The separate `linear` gain nodes and
their links are gone; topology is back to copy → convolver. Regression
tests assert the muted channel renders `gain = 0.00000` on both
convolvers. **Still to confirm on hardware** that this form attenuates
(it should — `gain` scales the IR directly).

### Verified vs. hardware-pending for the editor
- **Verified offscreen:** the editor widget, radial-drag → dB mapping,
  bounds/clamping, presets, reset/undo, persistence + migration,
  keyboard operation (selection + level + M/S/T), the batched/solo
  command payloads, and that the rendered PipeWire conf uses control
  ports with the right multipliers.
- **Hardware-pending (Fedora + headset):** that the `Mult` control
  actually attenuates the intended channel, that respawn latency is
  acceptable in practice, and that `pw-play --channel-map` lands the
  test tone on the intended speaker. Implemented to the proven respawn
  pattern and compiled/tested for structure, but audible correctness is
  **not** verifiable on this box — do the mute-FL-and-test check on
  target.

### Honest future paths (not done, not claimed)
- **Glitch-free live level:** once the `Mult` control form is confirmed
  on hardware, levels could be set at runtime via
  `pw-cli set-param <node> Props { params = [ "<ch>_gain:Mult" <v> ] }`
  instead of respawning — enabling continuous drag with no glitch. Left
  as a follow-up because the exact syntax needs on-hardware verification.
- **Real draggable direction:** would require a different backend —
  PipeWire's `sofa` spatializer (libmysofa, `.sofa` HRTFs) exposes
  runtime-adjustable azimuth/elevation. That is the honest path if
  free-positioning is ever wanted; the current static-HRIR convolver
  cannot do it, which is why this editor does not pretend to.
