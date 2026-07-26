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

### Backend truth (what the DSP can and cannot do)
_To be filled in from the surround_chain / filter_chain inspection._

Preliminary (pending confirmation in code):
- The surround chain is **HRIR convolution**: each 5.1/7.1 channel is
  convolved with a fixed impulse response and summed to binaural
  stereo. The **direction** of each channel is baked into the HRIR and
  **cannot** be changed by moving a speaker — only a different HRIR
  changes direction. Faking azimuth/elevation would be dishonest.
- What a static convolver setup **can** honestly control per channel:
  **level (gain)** and **delay** (time alignment / perceived distance),
  and channel **mute/solo**. These map to genuine radial "distance"
  and loudness, not free azimuth.

The editor will expose only the truthful controls and clearly document
what is fixed by the sound profile. Details land here as the work
proceeds.
