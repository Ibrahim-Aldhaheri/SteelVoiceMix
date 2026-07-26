"""Every screen constructs, renders, and survives a resize in both themes.

This is the safety net that 'py_compile passes' never was: a broken
layout, a bad signal connection, or a widget that throws on show would
pass compilation but fail here.
"""
from __future__ import annotations

import pytest

from tests.conftest import pump

PAGES = ["home", "sinks", "equalizer", "surround", "microphone", "deck", "settings"]
SIZES = [(820, 660), (1180, 880), (1500, 1000)]


def test_all_pages_render_all_sizes(window, qapp):
    win, _ = window
    for i in range(len(PAGES)):
        win.nav.setCurrentRow(i)
        for w, h in SIZES:
            win.resize(w, h)
            pump(qapp)
            # grab() forces a full layout + paint; a layout that can't
            # resolve raises here.
            assert not win.grab().isNull()


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_both_themes(qapp, stub_daemon, theme):
    from gui.main_window import MixerGUI
    from gui.theme import apply_theme

    apply_theme(theme)
    win = MixerGUI()
    win.show()
    pump(qapp)
    for i in range(len(PAGES)):
        win.nav.setCurrentRow(i)
        pump(qapp)
        assert not win.grab().isNull()
    win.close()
    pump(qapp)


def test_nav_has_every_page(window):
    win, _ = window
    assert win.nav.count() == len(PAGES)
    assert set(win._page_index) == set(PAGES)
