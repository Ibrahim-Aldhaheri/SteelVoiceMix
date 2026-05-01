"""Shared visual primitives used across tab modules.

Lives at the top of `gui/` so each tab can import without going through
the main window. Keep this module free of business logic — it's only
for stylesheet, layout helpers, and small visual widgets like the
animated ToggleSwitch.
"""

from __future__ import annotations

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QPointF,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
)
from PySide6.QtGui import QColor, QIcon, QPainter
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QProgressBar,
    QSlider,
    QVBoxLayout,
    QWidget,
)

APP_ICON = "steelvoicemix"
APP_ICON_FALLBACK = "audio-headset"

# Single accent colour used for active states, the sidebar selection
# stripe, and the connection-status pill. Kept as a constant rather
# than baked into the QSS so we can colour-coordinate widgets that draw
# themselves (ToggleSwitch) with widgets styled via QSS.
ACCENT = "#4CAF50"
ACCENT_DIM = "#357935"
WARN = "#FF9800"
ERROR = "#F44336"


# Global stylesheet — gives the window a more polished look than plain
# Qt while still respecting the system palette. Cards (QFrame#card),
# the sidebar nav, and section titles are the three main pieces; the
# rest is incremental polish on stock widgets.
GLOBAL_QSS = f"""
QMainWindow {{
    background-color: palette(window);
}}

/* --- Sidebar nav --------------------------------------------------- */
QListWidget#sidebar {{
    background: palette(window);
    border: none;
    border-right: 1px solid palette(mid);
    outline: none;
    padding: 8px 0;
    font-size: 13px;
}}
QListWidget#sidebar::item {{
    padding: 12px 16px;
    color: palette(text);
    border-left: 3px solid transparent;
    margin-right: 1px;
}}
QListWidget#sidebar::item:hover {{
    background: palette(midlight);
}}
QListWidget#sidebar::item:selected {{
    background: palette(midlight);
    border-left: 3px solid {ACCENT};
    color: palette(text);
    font-weight: bold;
}}

/* --- Card sections ------------------------------------------------- */
QFrame#card {{
    background: palette(base);
    border: 1px solid palette(mid);
    border-radius: 8px;
}}
QLabel#section-title {{
    font-weight: bold;
    font-size: 11px;
    color: palette(placeholder-text);
    text-transform: uppercase;
    letter-spacing: 1.5px;
    padding-bottom: 2px;
}}

/* --- Status pill (header) ----------------------------------------- */
QLabel#status-pill {{
    padding: 6px 14px;
    border-radius: 12px;
    background: palette(midlight);
    border: 1px solid palette(mid);
    font-size: 12px;
    font-weight: bold;
}}
QLabel#status-pill[state="ok"] {{
    color: {ACCENT};
    border-color: {ACCENT};
}}
QLabel#status-pill[state="bad"] {{
    color: {ERROR};
    border-color: {ERROR};
}}

/* --- Buttons ------------------------------------------------------- */
QPushButton {{
    padding: 6px 14px;
    border-radius: 6px;
    border: 1px solid palette(mid);
    background: palette(button);
    min-height: 24px;
}}
QPushButton:hover {{
    background: palette(midlight);
    border-color: {ACCENT};
}}
QPushButton:pressed {{
    background: palette(mid);
}}
QPushButton:disabled {{
    color: palette(placeholder-text);
    border-color: palette(midlight);
}}
QPushButton:flat {{
    border: none;
    background: transparent;
}}
QPushButton:flat:hover {{
    background: palette(midlight);
}}

/* --- Combos -------------------------------------------------------- */
QComboBox, QLineEdit {{
    padding: 5px 10px;
    border: 1px solid palette(mid);
    border-radius: 6px;
    min-height: 22px;
    selection-background-color: {ACCENT};
}}
QComboBox:focus, QLineEdit:focus {{
    border-color: {ACCENT};
}}

/* --- Misc ---------------------------------------------------------- */
QCheckBox {{
    spacing: 8px;
}}
QFrame[divider="true"] {{
    background: palette(mid);
    max-height: 1px;
    min-height: 1px;
    margin: 4px 0;
}}
QSlider::groove:vertical {{
    background: palette(midlight);
    border: 1px solid palette(mid);
    width: 6px;
    border-radius: 3px;
}}
QSlider::handle:vertical {{
    background: {ACCENT};
    border: 2px solid palette(window);
    height: 18px;
    width: 18px;
    margin: 0 -8px;
    border-radius: 9px;
}}
QSlider::handle:vertical:hover {{
    background: {ACCENT_DIM};
}}
QSlider::sub-page:vertical {{
    background: palette(midlight);
    border-radius: 3px;
}}
QSlider::add-page:vertical {{
    background: {ACCENT};
    border-radius: 3px;
}}
"""


# Canonical settings key → exact display string used in the position combo.
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
        "QProgressBar { border: 1px solid palette(mid); border-radius: 6px; "
        "height: 22px; text-align: center; background: palette(base); }"
        f"QProgressBar::chunk {{ background: {chunk_color}; border-radius: 5px; }}"
    )
    return bar


def card(title: str | None, *contents) -> QFrame:
    """Wrap a set of widgets / layouts in a card-styled QFrame.

    Use this instead of dropping a `section_title` + content directly
    into a tab page — the QFrame#card QSS rule paints the background
    and border, so every section gets the same chrome for free. `title`
    can be `None` for an unlabelled card. Pass child widgets and/or
    QLayouts as positional args; they're added in order with the
    standard 8 px spacing.
    """
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(14, 12, 14, 14)
    layout.setSpacing(8)
    if title:
        layout.addWidget(section_title(title))
    for item in contents:
        if isinstance(item, QLayout):
            layout.addLayout(item)
        elif isinstance(item, QWidget):
            layout.addWidget(item)
        else:
            raise TypeError(f"card() doesn't know how to add {type(item)}")
    return frame


# --------------------------------------------------------- ToggleSwitch
#
# QCheckBox subclass that paints itself as an iOS-style sliding toggle.
# We inherit from QCheckBox specifically so all the existing toggled
# signal wiring (on every tab) keeps working — drop-in replacement
# wherever a checkbox was being used as an on/off switch.


class ToggleSwitch(QCheckBox):
    """Animated on/off switch. Same `toggled` signal as QCheckBox.

    Paints a rounded pill (track) with a smaller circle (knob) that
    slides between left (off) and right (on) positions when the user
    clicks. The track turns the accent colour when on, grey when off.
    Disabled state dims both colours so the user can tell at a glance
    that the control is inactive.
    """

    _OFF_TRACK = QColor("#9e9e9e")
    _ON_TRACK = QColor(ACCENT)
    _KNOB = QColor("#ffffff")
    _DISABLED_ALPHA = 90

    def __init__(self, parent=None):
        super().__init__(parent)
        # We paint everything ourselves — strip the default QCheckBox
        # indicator + label so the bounding rect is purely the toggle.
        self.setText("")
        self.setCursor(Qt.PointingHandCursor)
        # Internal knob X position, updated by the property animation.
        self._knob_x: float = self._knob_left()
        self._anim = QPropertyAnimation(self, b"knob_x", self)
        self._anim.setDuration(160)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self.toggled.connect(self._on_toggled)

    # Sizing — QCheckBox's default sizeHint includes room for a label
    # + indicator. Override so layout only allocates the toggle's own
    # rectangle.
    def sizeHint(self) -> QSize:
        return QSize(46, 24)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    # The default QCheckBox hitTest is also tied to the indicator
    # position — replace it with the full widget rect so anywhere on
    # the toggle is clickable.
    def hitButton(self, pos) -> bool:
        return self.rect().contains(pos)

    # The animated property — Qt's animation framework reads/writes
    # this via `b"knob_x"` and the get/set pair below.
    def _get_knob_x(self) -> float:
        return self._knob_x

    def _set_knob_x(self, value: float) -> None:
        self._knob_x = value
        self.update()

    knob_x = Property(float, _get_knob_x, _set_knob_x)

    def _on_toggled(self, checked: bool) -> None:
        target = self._knob_right() if checked else self._knob_left()
        self._anim.stop()
        self._anim.setStartValue(self._knob_x)
        self._anim.setEndValue(target)
        self._anim.start()

    def _knob_radius(self) -> float:
        # Slightly smaller than the track so there's a 3 px margin all
        # round when the knob is at either extreme.
        return self.height() / 2.0 - 3.0

    def _knob_left(self) -> float:
        return 3.0

    def _knob_right(self) -> float:
        return self.width() - self.height() + 3.0

    # Re-snap to the correct extreme on resize so we don't end up with
    # the knob in some random middle position after layout settles.
    def resizeEvent(self, event) -> None:
        self._knob_x = self._knob_right() if self.isChecked() else self._knob_left()
        super().resizeEvent(event)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w = self.width()
        h = self.height()
        radius = h / 2.0

        track_color = QColor(self._ON_TRACK if self.isChecked() else self._OFF_TRACK)
        if not self.isEnabled():
            track_color.setAlpha(self._DISABLED_ALPHA)
        p.setBrush(track_color)
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(QRectF(0, 0, w, h), radius, radius)

        knob_color = QColor(self._KNOB)
        if not self.isEnabled():
            knob_color.setAlpha(180)
        kr = self._knob_radius()
        cx = self._knob_x + kr
        cy = h / 2.0
        p.setBrush(knob_color)
        p.drawEllipse(QPointF(cx, cy), kr, kr)


# ------------------------------------------------------- labelled toggle


def alpha_badge(text: str = "ALPHA", *, tooltip: str | None = None) -> QLabel:
    """Small orange pill used to mark unstable / experimental features.
    Same visual treatment as the badge in `labelled_toggle`, exposed
    as a free helper so it can sit alongside non-toggle controls (e.g.
    next to a button-style row in the Sinks tab)."""
    label = QLabel(text)
    label.setStyleSheet(
        f"background: {WARN};"
        "color: white;"
        "font-size: 9px;"
        "font-weight: bold;"
        "padding: 2px 6px;"
        "border-radius: 4px;"
    )
    if tooltip:
        label.setToolTip(tooltip)
    return label


class NoWheelComboBox(QComboBox):
    """QComboBox subclass that ignores mouse-wheel events.

    Stock QComboBox cycles through entries on scroll, even when the
    popup is closed. Combined with our auto-apply-on-change wiring
    (channel switch reloads the EQ tab; preset selection re-routes
    audio), an accidental scroll while the cursor is over the combo
    silently changes app behaviour without the user touching it.
    Using this subclass instead is a one-character swap that
    eliminates the surprise."""

    def wheelEvent(self, event) -> None:
        event.ignore()


class NoWheelSlider(QSlider):
    """QSlider subclass that ignores mouse-wheel events. Same
    reasoning as NoWheelComboBox: stock QSlider's wheel scroll
    nudges the value, and the EQ-band sliders fork the active
    preset to a fresh Custom-N on any change. Users scrolling
    over the EQ tab kept losing their preset selection. Using
    this subclass is a drop-in fix wherever the slider's value
    is committed to a daemon command."""

    def wheelEvent(self, event) -> None:
        event.ignore()


def labelled_toggle(
    text: str,
    *,
    tooltip: str | None = None,
    badge: str | None = None,
) -> tuple[QHBoxLayout, ToggleSwitch]:
    """A horizontal row with a label on the left and a ToggleSwitch on
    the right. Returns (layout, toggle) so the caller can connect to
    the toggle's `toggled` signal and add the layout into a card or
    parent layout.

    Optional `badge` adds a small coloured pill between the label and
    the toggle — used to mark unstable / alpha features so users know
    the toggle isn't quite production-ready. Currently uses the WARN
    accent (orange) for visibility."""
    row = QHBoxLayout()
    row.setSpacing(10)
    label = QLabel(text)
    if tooltip:
        label.setToolTip(tooltip)
    row.addWidget(label, 1)
    if badge:
        badge_lbl = QLabel(badge)
        badge_lbl.setStyleSheet(
            f"background: {WARN};"
            "color: white;"
            "font-size: 9px;"
            "font-weight: bold;"
            "padding: 2px 6px;"
            "border-radius: 4px;"
        )
        if tooltip:
            badge_lbl.setToolTip(tooltip)
        row.addWidget(badge_lbl, 0, alignment=Qt.AlignVCenter)
    toggle = ToggleSwitch()
    if tooltip:
        toggle.setToolTip(tooltip)
    row.addWidget(toggle, 0, alignment=Qt.AlignVCenter)
    return row, toggle
