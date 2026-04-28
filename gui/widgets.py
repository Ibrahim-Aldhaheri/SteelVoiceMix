"""Shared visual primitives used across tab modules.

Lives at the top of `gui/` so each tab can import without going through
the main window. Keep this module free of business logic — it's only for
QSS, small label/divider helpers, and constant lookups."""

from __future__ import annotations

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QFrame, QLabel, QProgressBar

APP_ICON = "steelvoicemix"
APP_ICON_FALLBACK = "audio-headset"


# Global stylesheet — gives the window a more cohesive look without
# overriding the user's system theme too aggressively. Most of these
# rules just tighten spacing, give buttons consistent padding, and
# soften borders. The progress bars keep their explicit per-bar styles
# (chunk colours) — those override these defaults where needed.
GLOBAL_QSS = """
QMainWindow {
    background-color: palette(window);
}
QTabWidget::pane {
    border: 1px solid palette(mid);
    border-radius: 6px;
    background: palette(base);
    top: -1px;
}
QTabBar::tab {
    background: palette(window);
    border: 1px solid palette(mid);
    border-bottom: none;
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
    padding: 6px 14px;
    min-width: 60px;
    color: palette(text);
}
QTabBar::tab:selected {
    background: palette(base);
    font-weight: bold;
}
QTabBar::tab:!selected:hover {
    background: palette(midlight);
}
QPushButton {
    padding: 5px 12px;
    border-radius: 4px;
    border: 1px solid palette(mid);
    background: palette(button);
    min-height: 22px;
}
QPushButton:hover {
    background: palette(midlight);
}
QPushButton:pressed {
    background: palette(mid);
}
QPushButton:disabled {
    color: palette(placeholder-text);
}
QPushButton:flat {
    border: none;
    background: transparent;
}
QComboBox {
    padding: 4px 8px;
    border: 1px solid palette(mid);
    border-radius: 4px;
    min-height: 22px;
}
QCheckBox {
    spacing: 8px;
}
QLabel#section-title {
    font-weight: bold;
    font-size: 11px;
    color: palette(placeholder-text);
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-top: 4px;
}
QFrame[divider="true"] {
    background: palette(mid);
    max-height: 1px;
    min-height: 1px;
    margin: 4px 0;
}
"""


# Canonical settings key → exact display string used in the position combo.
# Avoid using .replace("-", " ").title() to derive this — the items in the
# combo keep the dash, so a space-separated lookup never matches and the
# selected index doesn't update on profile load (or on startup if the
# user's saved position isn't the default).
POSITION_DISPLAY: dict[str, str] = {
    "top-right": "Top-right",
    "top-left": "Top-left",
    "bottom-right": "Bottom-right",
    "bottom-left": "Bottom-left",
    "center": "Center",
}


def section_title(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("section-title")
    return label


def divider() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.HLine)
    line.setProperty("divider", True)
    return line


def app_icon() -> QIcon:
    """Return our installed icon, falling back to the generic theme icon
    when running from a source checkout that hasn't been installed yet."""
    return QIcon.fromTheme(APP_ICON, QIcon.fromTheme(APP_ICON_FALLBACK))


def make_bar(chunk_color: str) -> QProgressBar:
    bar = QProgressBar()
    bar.setRange(0, 100)
    bar.setValue(100)
    bar.setTextVisible(True)
    bar.setFormat("%v%")
    bar.setStyleSheet(
        "QProgressBar { border: 1px solid palette(mid); border-radius: 4px; "
        "height: 22px; text-align: center; }"
        f"QProgressBar::chunk {{ background: {chunk_color}; border-radius: 3px; }}"
    )
    return bar
