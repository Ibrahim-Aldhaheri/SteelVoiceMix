"""Every screen constructs, renders, and survives a resize in both themes.

This is the safety net that 'py_compile passes' never was: a broken
layout, a bad signal connection, or a widget that throws on show would
pass compilation but fail here.
"""
from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont

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


def test_pages_render_with_200_percent_text(qapp, stub_daemon):
    from gui.main_window import MixerGUI

    original = QFont(qapp.font())
    enlarged = QFont(original)
    enlarged.setPointSizeF(max(18.0, original.pointSizeF() * 2.0))
    qapp.setFont(enlarged)
    win = MixerGUI()
    win.resize(1180, 880)
    win.show()
    try:
        for i in range(len(PAGES)):
            win.nav.setCurrentRow(i)
            pump(qapp)
            assert not win.grab().isNull()
            scroller = win.stack.currentWidget()
            assert scroller.horizontalScrollBar().maximum() >= 0
    finally:
        win.close()
        qapp.setFont(original)
        pump(qapp)


def test_pages_render_right_to_left(window, qapp):
    win, _ = window
    win.setLayoutDirection(Qt.RightToLeft)
    for i in range(len(PAGES)):
        win.nav.setCurrentRow(i)
        pump(qapp)
        assert not win.grab().isNull()
    assert win.layoutDirection() == Qt.RightToLeft


@pytest.mark.parametrize("normal", ["#1c1c20", "#e8e8eb"])
def test_resolved_theme_icons_use_requested_contrast(qapp, normal):
    from PySide6.QtGui import QColor, QIcon, QPixmap
    from gui.widgets import ON_ACCENT, _palette_tinted_icon

    source_pm = QPixmap(22, 22)
    source_pm.fill(QColor("white"))
    icon = _palette_tinted_icon(QIcon(source_pm), QColor(normal))

    for mode, expected in (
        (QIcon.Normal, QColor(normal)),
        (QIcon.Active, QColor(normal)),
        (QIcon.Selected, QColor(ON_ACCENT)),
    ):
        image = icon.pixmap(22, 22, mode, QIcon.Off).toImage()
        assert image.pixelColor(11, 11) == expected
