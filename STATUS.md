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

### Verified vs. hardware-pending for the editor
- **Verified offscreen:** the editor widget, radial-drag → dB mapping,
  bounds/clamping, presets, reset/undo, persistence + migration,
  keyboard operation, and the command payloads the GUI emits.
- **Hardware-pending (Fedora + headset):** that the daemon's new
  per-channel gain nodes actually attenuate the right channel, that
  respawn latency is acceptable in practice, and that test-tone routing
  hits the intended speaker. Implemented to the proven respawn pattern
  and compiled, but audio correctness is **not** verifiable on this box.
