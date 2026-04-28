"""Generate short reference clips for ear-testing the EQ.

Everything here is synthesised at runtime — pure stdlib (`math`,
`random`, `wave`, `struct`) — so we ship no audio assets and have no
licensing questions to worry about.

Clips are written into `$XDG_RUNTIME_DIR/steelvoicemix/test-audio/`
with stable filenames. Re-playing the same clip type reuses the file
on disk (the synthesis takes ~100 ms for a 5 s clip in CPython, fast
enough to regenerate on every Play, but stable filenames keep tmpfs
churn down).

Channel mapping: each EQ channel corresponds to a managed null-sink,
so we just play into that sink. Audio then flows null-sink → loopback
→ EQ chain (if enabled) → headset / HDMI, exactly the same path real
content would take. The user hears whatever the EQ is shaping.
"""

from __future__ import annotations

import logging
import math
import os
import random
import struct
import wave
from pathlib import Path

log = logging.getLogger(__name__)

SAMPLE_RATE = 48000  # PipeWire default — clean sines up to 20 kHz.

# Channel key → user-facing PipeWire sink name. Mirrors the constants
# in src/audio.rs; if those rename, this needs to follow.
CHANNEL_TO_SINK: dict[str, str] = {
    "game": "SteelGame",
    "chat": "SteelChat",
    "media": "SteelMedia",
    "hdmi": "SteelHDMI",
}


def tmp_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    d = Path(base) / "steelvoicemix" / "test-audio"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------- writer


def _write_wav(samples: list[float], filename: str) -> Path:
    """Encode `samples` (mono floats in [-1, 1]) as 16-bit signed PCM
    and write to `tmp_dir() / filename`. Returns the path."""
    path = tmp_dir() / filename
    # Clip + scale outside the loop so the inner pack stays simple.
    pcm = struct.pack(
        f"<{len(samples)}h",
        *(int(max(-1.0, min(1.0, s)) * 32767) for s in samples),
    )
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm)
    return path


# ------------------------------------------------------------ generators


def pink_noise(duration_s: float = 5.0) -> Path:
    """Voss-McCartney pink noise — equal energy per octave. The
    standard reference signal for EQ tuning: when you boost a band, you
    literally hear that frequency range get louder relative to the rest
    of the spectrum."""
    n_samples = int(duration_s * SAMPLE_RATE)
    rows = 16
    state = [random.uniform(-1.0, 1.0) for _ in range(rows)]
    running = sum(state)
    out = []
    counter = 0
    # Voss-McCartney trick: on each step pick the row whose update bit
    # just flipped (low bit of `counter` tells us which one). Sum all
    # rows + a white-noise component, then normalise.
    norm = 1.0 / (rows + 1)
    for _ in range(n_samples):
        counter += 1
        # Index of lowest set bit; runs from 0 up, with frequency
        # halving for each higher index — that's what gives us pink.
        bit = (counter & -counter).bit_length() - 1
        if bit < rows:
            new_val = random.uniform(-1.0, 1.0)
            running += new_val - state[bit]
            state[bit] = new_val
        out.append((running + random.uniform(-1.0, 1.0)) * norm)
    return _write_wav(out, "pink-noise.wav")


def white_noise(duration_s: float = 5.0) -> Path:
    """Uniformly-distributed white noise — flat power spectrum. Less
    useful than pink for octave-by-octave EQ judgement (high
    frequencies dominate perceptually) but handy as a contrast."""
    n_samples = int(duration_s * SAMPLE_RATE)
    out = [random.uniform(-0.3, 0.3) for _ in range(n_samples)]
    return _write_wav(out, "white-noise.wav")


def sine_sweep(duration_s: float, f_start: float, f_end: float) -> Path:
    """Logarithmic sine sweep from f_start to f_end Hz. Hearing the
    sweep makes the EQ shape audible: any band that's boosted stands
    out as the swept tone passes through it.

    Phase formula for a log sweep, from Farina (2000): the instantaneous
    phase that yields f(t) = f0 * (f1/f0)^(t/T) integrates to
    phi(t) = 2π * f0 * T / ln(f1/f0) * ((f1/f0)^(t/T) - 1)."""
    if f_start <= 0 or f_end <= 0 or f_start == f_end:
        raise ValueError("sweep frequencies must be positive and distinct")
    n_samples = int(duration_s * SAMPLE_RATE)
    ratio = f_end / f_start
    k = duration_s / math.log(ratio)
    out = []
    # Soft fade in/out (10 ms each side) so the sweep doesn't click on
    # start/stop. At log frequency, the start is a barely-audible
    # ~20 Hz rumble — fade-in is mostly belt-and-suspenders.
    fade_n = int(0.010 * SAMPLE_RATE)
    for i in range(n_samples):
        t = i / SAMPLE_RATE
        phase = 2.0 * math.pi * f_start * k * (math.exp(t / k) - 1.0)
        s = 0.5 * math.sin(phase)
        if i < fade_n:
            s *= i / fade_n
        elif i > n_samples - fade_n:
            s *= (n_samples - i) / fade_n
        out.append(s)
    safe_lo = int(round(f_start))
    safe_hi = int(round(f_end))
    return _write_wav(out, f"sweep-{safe_lo}-{safe_hi}.wav")


def tone(freq: float, duration_s: float = 3.0) -> Path:
    """Pure sine at `freq` Hz with 50 ms attack/release fades to keep
    the start and stop click-free. Useful to test individual EQ bands
    in isolation — set the band to 0 dB, play the tone, then move the
    band and listen for the level change."""
    if freq <= 0:
        raise ValueError("tone frequency must be positive")
    n_samples = int(duration_s * SAMPLE_RATE)
    fade_n = int(0.050 * SAMPLE_RATE)
    out = []
    omega = 2.0 * math.pi * freq
    for i in range(n_samples):
        t = i / SAMPLE_RATE
        s = 0.5 * math.sin(omega * t)
        if i < fade_n:
            s *= i / fade_n
        elif i > n_samples - fade_n:
            s *= (n_samples - i) / fade_n
        out.append(s)
    return _write_wav(out, f"tone-{int(round(freq))}hz.wav")


# ------------------------------------------------------------ catalogue
#
# (label, factory) pairs — drives the type combo on the EQ tab. Order
# is the display order. Tone presets are spaced roughly one octave
# apart in the audible range so the user can probe each EQ band by
# picking the closest matching tone.

TEST_AUDIO_CATALOGUE: list[tuple[str, "callable[[], Path]"]] = [
    ("Pink noise (5 s)", lambda: pink_noise(5.0)),
    ("White noise (5 s)", lambda: white_noise(5.0)),
    ("Sweep 20 Hz – 20 kHz (10 s)", lambda: sine_sweep(10.0, 20.0, 20000.0)),
    ("Sweep low 20 Hz – 2 kHz (10 s)", lambda: sine_sweep(10.0, 20.0, 2000.0)),
    ("Sweep high 2 kHz – 20 kHz (10 s)", lambda: sine_sweep(10.0, 2000.0, 20000.0)),
    ("Tone 100 Hz (3 s)", lambda: tone(100.0)),
    ("Tone 250 Hz (3 s)", lambda: tone(250.0)),
    ("Tone 1 kHz (3 s)", lambda: tone(1000.0)),
    ("Tone 4 kHz (3 s)", lambda: tone(4000.0)),
    ("Tone 10 kHz (3 s)", lambda: tone(10000.0)),
]
