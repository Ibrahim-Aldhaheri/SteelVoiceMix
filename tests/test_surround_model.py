"""Unit tests for the surround stage model — pure logic, no Qt."""
from __future__ import annotations

import math

from gui.surround_model import (
    GAIN_MAX_DB,
    GAIN_MIN_DB,
    apply_preset,
    clamp_db,
    db_to_fraction,
    db_to_linear,
    default_channels,
    deserialize,
    fraction_to_db,
    serialize,
)


def test_default_channels_layout():
    ch = default_channels()
    assert set(ch) == {"fl", "fr", "fc", "lfe", "sl", "sr", "rl", "rr"}
    # Direction is fixed by the standard layout; assert the anchors.
    assert ch["fc"].azimuth == 0.0
    assert ch["fl"].azimuth == -30.0
    assert ch["fr"].azimuth == 30.0
    assert ch["sl"].azimuth == -90.0 and ch["sr"].azimuth == 90.0
    assert ch["rl"].azimuth == -150.0 and ch["rr"].azimuth == 150.0
    assert ch["lfe"].positional is False


def test_clamp_bounds_and_non_finite():
    assert clamp_db(1e308) == GAIN_MAX_DB
    assert clamp_db(-1e308) == GAIN_MIN_DB
    assert clamp_db(float("nan")) == 0.0
    assert clamp_db(float("inf")) == 0.0
    assert clamp_db("x") == 0.0


def test_db_fraction_roundtrip():
    for db in (-24, -12, -6, 0, 3, 6):
        f = db_to_fraction(db)
        assert 0.35 <= f <= 1.0
        assert abs(fraction_to_db(f) - db) < 0.01


def test_louder_is_closer():
    # Higher level => smaller radius (closer to the listener).
    assert db_to_fraction(6) < db_to_fraction(0) < db_to_fraction(-24)


def test_db_to_linear():
    assert abs(db_to_linear(0) - 1.0) < 1e-6
    assert abs(db_to_linear(-6) - 0.501) < 0.01
    assert abs(db_to_linear(6) - 1.995) < 0.01


def test_presets_absolute_and_touch_only_level():
    ch = default_channels()
    ch["fc"].gain_db = 5.0  # dirty it
    apply_preset(ch, "flat")
    assert all(abs(c.gain_db) < 1e-6 for c in ch.values())

    apply_preset(ch, "dialogue")
    assert ch["fc"].gain_db == 4.0
    assert ch["rl"].gain_db == -3.0
    # azimuths (direction) are never touched by presets
    assert ch["fc"].azimuth == 0.0

    apply_preset(ch, "5.1")
    assert ch["sl"].muted and ch["sr"].muted


def test_serialize_roundtrip_and_migration():
    ch = default_channels()
    ch["fc"].gain_db = -6.0
    ch["fc"].muted = True
    data = serialize(ch)
    assert data["fc"] == {"gain_db": -6.0, "muted": True}
    back = deserialize(data)
    assert back["fc"].gain_db == -6.0 and back["fc"].muted is True
    # Legacy / missing state migrates to defaults, never raises.
    assert deserialize(None)["fc"].gain_db == 0.0
    assert deserialize({"garbage": 1})["fr"].gain_db == 0.0
    # A partial dict fills the rest with defaults.
    partial = deserialize({"fl": {"gain_db": 3.0}})
    assert partial["fl"].gain_db == 3.0 and partial["rr"].gain_db == 0.0
