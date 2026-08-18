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

## UI/UX improvement gate — local contracts and pending Fedora evidence

| Area | Change | Verified how |
|---|---|---|
| Status semantics | Header names the background service; Home independently reports headset availability; configured effects are shown as “Set up” rather than running while hardware is unavailable | offscreen state-transition tests |
| Sinks | Media/HDMI remain primary; routing, boost, and redirect controls retain state and commands behind an accessible advanced disclosure | render + interaction tests |
| Equalizer | Presets and bands remain primary; Test Audio, Listen, and Auto Game-EQ are grouped under Testing & automation | render + interaction tests |
| Surround | Intentional three-step empty state leads profile → enable → app routing; fixed HRIR directions remain explicit | render + interaction tests |
| Settings | Existing two-column structure retained; shortcut detail and advanced/maintenance actions are disclosed on demand | render + interaction tests |
| Accessibility | Higher-contrast help copy, explicit disclosure and glyph-control names, label buddies, focus styling, RTL and enlarged-text render contracts | offscreen pytest; not screen-reader certification |
| Deck safety | Hardware management remains default-off and controls require both device presence and explicit opt-in | interaction regression test + Rust defaults |

The repository offscreen assertions are committed. The headless Ubuntu checkout
does not provide PySide6, so `scripts/check.sh` reports that pytest portion as a
documented skip there. A separate isolated Fedora 44 KDE acceptance run used the
system PySide6 with a temporary HOME/config/runtime, stubbed daemon/update/game
watcher paths, the Breeze Qt style, and no live audio or device access.

### Fedora KDE isolated acceptance
- **Verified:** Breeze style/icon resolution and icon contrast in light/dark.
- **Verified:** every page at 820 and 1180 logical pixels with actual 2× Qt
  scaling, including reachable scroll overflow.
- **Verified:** Arabic/RTL mirroring with expanded long text, keyboard focus and
  Space activation of disclosures, and key accessible-name/state contracts.
- **Verified:** synthetic service-ready/headset-off semantics and Deck controls
  default-off/disabled with no hardware authorization.

This is a strong isolated UI acceptance, not full screen-reader certification.
Live daemon, PipeWire, HID, headset, and Deck behavior were deliberately outside
this UX gate and were not exercised.

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
convolvers.

**CONFIRMED on hardware** (Fedora + Nova Pro, beta3) — see the
measurement rig below:

| state | L peak | R peak |
|---|---|---|
| FL burst, baseline | 0.4959 | 0.2548 |
| FL burst, FL muted | **0.0000** | **0.0000** |
| FL burst, restored | 0.4959 | 0.2548 |

### Measurement rig — surround DSP is now objectively testable

Audible correctness no longer depends on someone's ears. Play a WAV
carrying signal on exactly one of the eight channels into the chain and
record the chain's own binaural output:

```bash
pw-record --target=effect_output.SteelSurround /tmp/out.wav &
pw-play  --target=effect_input.SteelSurround  /tmp/burst_fl.wav
# then compare per-channel peaks in /tmp/out.wav
```

Do **not** measure at `alsa_output.…:monitor` — that path reports both
captured channels identically (a hard-left probe tone read equal on L
and R), so it cannot see the binaural asymmetry that is the entire
point. Tapping `effect_output.SteelSurround` reads true.

Reference values on a healthy chain (EAC_Default HRIR):

| single-channel burst | L/R peak ratio |
|---|---|
| FL | 2.61 (left) |
| SR | 0.36 (right) |
| FC | 1.04 (centred) |

### Test tone: was inaudible/unlocalised → 8-channel burst

Reported as "click Test and nothing happens". The daemon *was* playing
the tone (journal + WAV timestamps confirm), but it was a 0.4 s, 440 Hz,
-12 dBFS **mono** file steered with `pw-play --channel-map=<POS>`.
Measured through the rig, that produced only a 1.38:1 L/R ratio for FL —
a mono stream into the 8-channel sink goes through PipeWire's channelmix,
which spreads it, so `--channel-map` only biases rather than places it.
440 Hz is also close to the worst probe for HRTF localisation, whose
cues live in spectral shaping and HF ITD/ILD.

Now the daemon writes an **8-channel WAVE_FORMAT_EXTENSIBLE** file with
an explicit `dwChannelMask` (FL FR FC LFE RL RR SL SR, matching the
chain's `audio.position`) carrying a 0.9 s broadband burst (24
log-spaced partials, 300 Hz – 8 kHz, peak-normalised to -6 dBFS) on the
target channel only, and plays it with no `--channel-map`. Verified on
hardware — the ratios in the table above are the new burst. Output peak
went 0.20 → 0.59 (+9 dB). `pw-play`'s exit status and stderr are now
logged instead of discarded, so a future silent failure is diagnosable.

### Respawn storm: a level tweak rebuilt four filter chains

The journal showed every gain/mute/solo command tearing down and
respawning the surround chain **and all three EQ chains**, ~8 s per
edit with the sink mutex held, so consecutive edits queued into a
rebuild storm. `respawn_surround_if_running` was calling
`rewire_all_headphone_channels()`, but a gain change doesn't move the
headphone-path target — it's still `effect_input.SteelSurround`. The EQ
chains survive the respawn; only their links into the (new) surround
node need re-issuing, which is exactly what `check_links_alive` does.
With EQ off the bare `module-loopback`s still must be reloaded, since a
pulse loopback doesn't re-resolve its sink. Separately, the surround
chain's spawn-time `pw-link` retry loop paid its settling delay once per
channel serially; both channels now retry in one loop with a shorter
first delay (150 ms, backing off).

### Verified vs. hardware-pending for the editor
- **Verified offscreen:** the editor widget, radial-drag → dB mapping,
  bounds/clamping, presets, reset/undo, persistence + migration,
  keyboard operation (selection + level + M/S/T), the batched/solo
  command payloads, and that the rendered PipeWire conf uses control
  ports with the right multipliers.
- **Verified on hardware (Fedora + headset):** mute fully attenuates the
  intended channel and only that channel; the HRIR places FL left, SR
  right and FC centred; the per-channel test burst lands on the
  requested speaker.
- **Still unverified:** long-run stability of the reduced-churn respawn
  path under rapid dragging.

## Open bug: intermittent echo (NOT diagnosed — do not claim fixed)

Reported symptom: audio sometimes develops an echo. Restarting the
daemon does **not** clear it; unplugging and replugging the base station
does. Started "a few months ago".

What that rules out: a daemon restart tears down every module and
filter-chain child it owns and rebuilds them, so the duplicated path
does not live in daemon-owned state. It survives in PipeWire's / the
device's view of the graph until the ALSA node is destroyed.

Evidence gathered so far, pre- and post-replug on target:

- **Graph structure is identical either side of the replug.** Exactly
  three `module-loopback`s, one filter-chain child per chain, no
  duplicate or stray links into the headset, one Arctis card with a
  single active output profile. The only diff is node ids renumbering.
- **The chain's binaural output is clean.** Recording
  `effect_output.SteelSurround` while playing a single-channel burst,
  pre-replug vs post-replug: peak 0.1848 vs 0.1815, active duration
  896 ms in both, and the envelope autocorrelation shows no repeat at
  any lag from 5–400 ms. So no doubled path existed at measurement time.

That means the echo was not reproducing during the measurement window —
it is intermittent, and both snapshots caught the good state. It does
**not** identify the cause.

What remains consistent with every symptom (survives a daemon restart,
dies on a USB replug): a second live path into the headset created by
something the daemon does not own, so a restart can't clear it but
destroying the ALSA node does. The link watchdog could only ever *add*
edges, so nothing removed such a path. It now prunes them — see below.
Also still possible, and **not** observable from software: something
downstream of PipeWire entirely (ALSA node state, or the base station's
own DSP), since the sink monitor taps what PipeWire *sends*, not what
the deck plays.

### Watchdog now prunes stray edges (candidate fix — NOT confirmed)

`check_links_alive` reconciles in both directions. The removal rule is
deliberately narrow: an edge is only a deletion candidate when its
**output port is one the daemon already claims** (`effect_output.Steel*`).
Within that scope a second destination is unambiguously wrong — our
chains are single-destination by design — while a user's own stream on
the headset, the mic capture edge, and anything WirePlumber wires
between unrelated nodes are never touched. Covered by unit tests
including a "must not prune foreign edges" case, and dry-run against the
live target graph: zero adds, zero removals on a healthy system.

This closes a real gap and matches the symptom profile, but it is a
**candidate**, not a confirmed fix — nothing has yet observed the echo
in the act. If it recurs, run `/tmp/svm-echo-dump.sh` **while it is
audible** and diff against `/tmp/svm-after-replug.txt`; the journal will
also now carry `pw-link unexpected edge — removing …` if the watchdog
saw and cleared a stray path.

### Honest future paths (not done, not claimed)
- **Glitch-free live level:** levels are still applied by regenerating
  the conf and respawning the chain, because the convolver's `gain` is a
  config-time parameter. A runtime path would need the level to move
  back onto a control port (e.g. a `mixer` input `Gain`) driven by
  `pw-cli set-param` — worth revisiting only with the measurement rig
  above to prove the control port actually attenuates, which is the
  check the `linear` node failed.
- **Real draggable direction:** would require a different backend —
  PipeWire's `sofa` spatializer (libmysofa, `.sofa` HRTFs) exposes
  runtime-adjustable azimuth/elevation. That is the honest path if
  free-positioning is ever wanted; the current static-HRIR convolver
  cannot do it, which is why this editor does not pretend to.
