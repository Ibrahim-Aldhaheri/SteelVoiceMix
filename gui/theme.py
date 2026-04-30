"""Theme management — Auto / Light / Dark.

`Auto` follows the system's colour scheme via `QApplication.styleHints()
.colorScheme()` (Qt 6.5+). When the user switches their desktop
between light and dark, our window flips with it. `Light` / `Dark`
override that with an explicit palette so users can diverge from the
system if they want.

Implementation deliberately stays light-touch: we install one of two
QPalettes on the app, no per-widget styling. Cards, sidebar, status
pill etc. already use `palette(...)` references in `widgets.GLOBAL_QSS`,
so swapping the palette propagates everywhere via Qt's normal style
resolution. No QSS rewrite needed.
"""

from __future__ import annotations

from typing import Literal

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

ThemeMode = Literal["auto", "light", "dark"]
THEME_MODES: tuple[ThemeMode, ...] = ("auto", "light", "dark")


def _light_palette() -> QPalette:
    p = QPalette()
    p.setColor(QPalette.Window, QColor(245, 245, 247))
    p.setColor(QPalette.WindowText, QColor(28, 28, 32))
    p.setColor(QPalette.Base, QColor(255, 255, 255))
    p.setColor(QPalette.AlternateBase, QColor(238, 238, 242))
    p.setColor(QPalette.Text, QColor(28, 28, 32))
    p.setColor(QPalette.Button, QColor(238, 238, 242))
    p.setColor(QPalette.ButtonText, QColor(28, 28, 32))
    p.setColor(QPalette.Highlight, QColor(76, 175, 80))
    p.setColor(QPalette.HighlightedText, QColor(255, 255, 255))
    p.setColor(QPalette.PlaceholderText, QColor(110, 110, 115))
    p.setColor(QPalette.Mid, QColor(200, 200, 205))
    p.setColor(QPalette.Midlight, QColor(228, 228, 232))
    p.setColor(QPalette.Light, QColor(255, 255, 255))
    p.setColor(QPalette.Dark, QColor(160, 160, 165))
    p.setColor(QPalette.Shadow, QColor(0, 0, 0, 60))
    p.setColor(QPalette.ToolTipBase, QColor(255, 255, 230))
    p.setColor(QPalette.ToolTipText, QColor(28, 28, 32))
    return p


def _dark_palette() -> QPalette:
    p = QPalette()
    p.setColor(QPalette.Window, QColor(28, 28, 32))
    p.setColor(QPalette.WindowText, QColor(232, 232, 235))
    p.setColor(QPalette.Base, QColor(20, 20, 24))
    p.setColor(QPalette.AlternateBase, QColor(36, 36, 40))
    p.setColor(QPalette.Text, QColor(232, 232, 235))
    p.setColor(QPalette.Button, QColor(48, 48, 54))
    p.setColor(QPalette.ButtonText, QColor(232, 232, 235))
    p.setColor(QPalette.Highlight, QColor(76, 175, 80))
    p.setColor(QPalette.HighlightedText, QColor(20, 20, 24))
    p.setColor(QPalette.PlaceholderText, QColor(140, 140, 148))
    p.setColor(QPalette.Mid, QColor(70, 70, 78))
    p.setColor(QPalette.Midlight, QColor(56, 56, 62))
    p.setColor(QPalette.Light, QColor(64, 64, 72))
    p.setColor(QPalette.Dark, QColor(15, 15, 18))
    p.setColor(QPalette.Shadow, QColor(0, 0, 0, 120))
    p.setColor(QPalette.ToolTipBase, QColor(48, 48, 54))
    p.setColor(QPalette.ToolTipText, QColor(232, 232, 235))
    return p


def _system_prefers_dark(app: QApplication) -> bool:
    """True if the desktop's colour scheme is dark. Uses Qt 6.5's
    `styleHints().colorScheme()` when available; falls back to the
    default-palette luminance heuristic for older Qt.

    The fallback isn't perfect (some themes have light buttons on a
    dark window) but covers the common cases. Worth replacing with
    a proper xdg-desktop-portal query if anyone cares enough."""
    hints = app.styleHints()
    cs_method = getattr(hints, "colorScheme", None)
    if callable(cs_method):
        try:
            cs = cs_method()
            return cs == Qt.ColorScheme.Dark
        except Exception:
            pass
    # Fallback: inspect the OS-default palette's window colour.
    # Stash and restore so the probe doesn't pollute the live app.
    saved = app.palette()
    app.setPalette(app.style().standardPalette())
    bg = app.palette().color(QPalette.Window)
    app.setPalette(saved)
    # Rec. 709 luma; <128 = dark.
    luma = 0.2126 * bg.red() + 0.7152 * bg.green() + 0.0722 * bg.blue()
    return luma < 128


def apply_theme(mode: ThemeMode) -> None:
    """Apply one of Auto / Light / Dark to the running QApplication.
    Called on startup and whenever the user changes the radio in
    Settings → Appearance."""
    app = QApplication.instance()
    if app is None:
        return
    if mode not in THEME_MODES:
        mode = "auto"
    if mode == "auto":
        # Use the system style's natural palette so KDE/GNOME theming
        # carries through (icon colours, hover effects, etc.). Then
        # the rest of our QSS uses palette(...) references that
        # follow whatever we end up with here.
        app.setPalette(app.style().standardPalette())
    else:
        app.setPalette(_dark_palette() if mode == "dark" else _light_palette())
    _refresh_stylesheets(app)


def _refresh_stylesheets(app: QApplication) -> None:
    """Force every top-level widget's QSS to re-resolve `palette(...)`
    references against the new palette. Qt does not auto-refresh
    parsed stylesheets when the application palette changes — the
    widget keeps rendering with the colours that were resolved at
    setStyleSheet() time. Re-setting the same stylesheet string
    triggers a fresh parse + repolish, which picks up the new palette."""
    for w in app.topLevelWidgets():
        qss = w.styleSheet()
        if qss:
            w.setStyleSheet(qss)
        s = w.style()
        s.unpolish(w)
        s.polish(w)


def normalize_mode(value: str) -> ThemeMode:
    v = (value or "").strip().lower()
    return v if v in THEME_MODES else "auto"
