"""Pure-logic unit tests — preset validation and auto-EQ matching.

No Qt needed. These lock down the behaviour the security pass and the
auto-EQ bug fix established, so a future refactor can't silently
reintroduce either.
"""
from __future__ import annotations

import math

from gui.eq_presets import NUM_BANDS, validate_bands


def _band(gain=0.0, freq=1000.0, q=1.0, btype="peaking"):
    return {"freq": freq, "q": q, "gain": gain, "type": btype, "enabled": True}


def test_validate_accepts_good_bands():
    bands = [_band() for _ in range(NUM_BANDS)]
    out = validate_bands(bands)
    assert out is not None and len(out) == NUM_BANDS


def test_validate_rejects_wrong_count():
    assert validate_bands([_band() for _ in range(5)]) is None


def test_validate_rejects_non_list():
    assert validate_bands("nope") is None
    assert validate_bands(None) is None


def test_validate_clamps_extreme_gain():
    bands = [_band() for _ in range(NUM_BANDS)]
    bands[0]["gain"] = 1e308
    bands[1]["gain"] = -1e308
    out = validate_bands(bands)
    assert out[0]["gain"] <= 30.0
    assert out[1]["gain"] >= -30.0


def test_validate_replaces_nan_and_inf():
    bands = [_band() for _ in range(NUM_BANDS)]
    bands[0]["gain"] = float("nan")
    bands[1]["freq"] = float("inf")
    out = validate_bands(bands)
    assert math.isfinite(out[0]["gain"])
    assert math.isfinite(out[1]["freq"])


def test_validate_unknown_type_falls_back():
    bands = [_band(btype="evil") for _ in range(NUM_BANDS)]
    out = validate_bands(bands)
    assert out[0]["type"] == "peaking"


def test_auto_eq_resolves_special_char_presets():
    """The Baldur's Gate 3 class of bug: display names with punctuation
    must resolve through the fuzzy matcher and find_preset_bands."""
    from gui.game_eq import find_preset_bands, match_asm_preset

    for runtime_name in [
        "bg3", "Baldur's Gate 3", "Assassin's Creed Valhalla",
        "Marvel's Spider-Man 2",
    ]:
        matched = match_asm_preset(runtime_name)
        assert matched, f"no match for {runtime_name!r}"
        bands = find_preset_bands(matched)
        assert bands and len(bands) == NUM_BANDS, \
            f"{runtime_name!r} matched {matched!r} but bands didn't resolve"
