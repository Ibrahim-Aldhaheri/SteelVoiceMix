"""Shared visual primitives used across tab modules.

Lives at the top of `gui/` so each tab can import without going through
the main window. Keep this module free of business logic — it's only
for stylesheet, layout helpers, and small visual widgets like the
animated ToggleSwitch.
"""

from __future__ import annotations

from typing import Callable, Iterable

from PySide6.QtCore import (
    Property,
    QCoreApplication,
    QEasingCurve,
    QObject,
    QPointF,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    QTimer,
)
from PySide6.QtGui import QColor, QIcon, QPainter, QPalette, QPen, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QProgressBar,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

APP_ICON = "steelvoicemix"
APP_ICON_FALLBACK = "audio-headset"
THEME_ICON_ROLE = int(Qt.UserRole) + 77

# Single accent colour used for active states, the sidebar selection
# stripe, and the connection-status pill. Kept as a constant rather
# than baked into the QSS so we can colour-coordinate widgets that draw
# themselves (ToggleSwitch) with widgets styled via QSS.
ACCENT = "#4CAF50"
# Text/glyph colour to use on top of an ACCENT fill. Hard-coded rather
# than palette(highlighted-text) because the accent is ours, not the
# system's — the contrast has to hold in both light and dark themes.
ON_ACCENT = "#0E1A0F"
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
    padding: 10px 8px;
    font-size: 13px;
}}
QListWidget#sidebar::item {{
    padding: 9px 10px;
    margin: 1px 0;
    border-radius: 6px;
    color: palette(text);
}}
QListWidget#sidebar::item:hover {{
    background: palette(midlight);
}}
/* Selected row reads as a filled pill rather than a left-edge tick —
   the tick was easy to miss against the card background. */
QListWidget#sidebar::item:selected {{
    background: {ACCENT};
    color: {ON_ACCENT};
    font-weight: bold;
}}

/* --- Card sections ------------------------------------------------- */
QFrame#card {{
    background: palette(base);
    border: 1px solid palette(mid);
    border-radius: 10px;
}}
/* Section titles were 11px uppercase at placeholder-text contrast,
   which made every heading recede and left the pages looking like one
   undifferentiated wall. Slightly larger, full text colour, and much
   less letter-spacing so they actually anchor their section. */
QLabel#section-title {{
    font-weight: bold;
    font-size: 12px;
    color: palette(text);
    letter-spacing: 0.6px;
    padding-bottom: 2px;
}}
/* Secondary explanatory copy. Kept small and dim, but callers should
   prefer widgets.help_text() so long paragraphs collapse behind a
   toggle instead of permanently occupying vertical space. */
QLabel#help-text {{
    color: palette(text);
}}
QLabel#field-label {{
    color: palette(text);
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
QPushButton:focus, QComboBox:focus, QLineEdit:focus,
QCheckBox:focus, QSlider:focus {{
    border: 2px solid {ACCENT};
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
/* Primary call-to-action. One per view at most. */
QPushButton[cta="true"] {{
    background: {ACCENT};
    border-color: {ACCENT};
    color: {ON_ACCENT};
    font-weight: bold;
}}
QPushButton[cta="true"]:hover {{
    background: {ACCENT_DIM};
    border-color: {ACCENT_DIM};
}}
/* Small round "?" that expands a help paragraph. */
QPushButton#help-toggle {{
    border: 1px solid palette(mid);
    border-radius: 9px;
    background: transparent;
    color: palette(placeholder-text);
    font-weight: bold;
    font-size: 11px;
    min-height: 18px;
    max-height: 18px;
    min-width: 18px;
    max-width: 18px;
    padding: 0;
}}
QPushButton#help-toggle:hover {{
    border-color: {ACCENT};
    color: {ACCENT};
}}
QPushButton#help-toggle:checked {{
    background: {ACCENT};
    border-color: {ACCENT};
    color: {ON_ACCENT};
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
/* Inline warning strip (missing plugin, alpha feature, etc). Reads as
   an advisory band instead of loose orange text on the card. */
QFrame#notice {{
    background: palette(alternate-base);
    border: 1px solid palette(mid);
    border-left: 3px solid {WARN};
    border-radius: 6px;
}}
QLabel#notice-text {{
    color: palette(text);
    font-size: 11px;
}}

/* Horizontal sliders had no rule at all, so they fell back to the
   platform default and looked unrelated to the vertical EQ sliders. */
QSlider::groove:horizontal {{
    background: palette(midlight);
    border: 1px solid palette(mid);
    height: 6px;
    border-radius: 3px;
}}
QSlider::sub-page:horizontal {{
    background: {ACCENT};
    border: 1px solid {ACCENT};
    border-radius: 3px;
}}
QSlider::handle:horizontal {{
    background: palette(base);
    border: 2px solid {ACCENT};
    height: 16px;
    width: 16px;
    margin: -6px 0;
    border-radius: 9px;
}}
QSlider::handle:horizontal:hover {{
    border-color: {ACCENT_DIM};
    background: {ACCENT};
}}
QSlider::handle:horizontal:disabled {{
    border-color: palette(mid);
    background: palette(button);
}}
QSlider::sub-page:horizontal:disabled {{
    background: palette(mid);
    border-color: palette(mid);
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

/* --- Scrollbars ---------------------------------------------------- */
/* The default chunky scrollbar drew a hard vertical rule down the edge
   of every page. Slim and self-effacing suits a settings surface. */
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: palette(mid);
    min-height: 32px;
    border-radius: 5px;
}}
QScrollBar::handle:vertical:hover {{
    background: palette(dark);
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: transparent;
}}
"""


def quick_action(icon_names: tuple[str, ...], title: str, subtitle: str):
    """A large, friendly shortcut button for the Home dashboard.

    Two lines — a bold title and a dim one-line description — behind a
    theme icon, so a non-technical user can see at a glance what each
    shortcut does rather than decoding a bare label. Returns the
    QPushButton; the caller connects `clicked`.
    """
    from PySide6.QtWidgets import QPushButton
    button = QPushButton()
    button.setCursor(Qt.PointingHandCursor)
    button.setIcon(themed_icon(*icon_names))
    button.setProperty("themeIconNames", list(icon_names))
    button.setIconSize(QSize(22, 22))
    button.setText(f"  {title}\n  {subtitle}")
    button.setStyleSheet(
        "QPushButton {"
        "  text-align: left; padding: 10px 12px; border-radius: 8px;"
        "  border: 1px solid palette(mid); background: palette(base);"
        "}"
        "QPushButton:hover {"
        f"  border-color: {ACCENT}; background: palette(alternate-base);"
        "}"
    )
    button.setMinimumHeight(52)
    return button


def stat_readout(value: str, label: str):
    """A big number over a small caption, for dashboard metrics.

    Returns `(container, value_label)` so the caller can update the
    value live. Used for battery %, ChatMix balance, etc.
    """
    from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget
    box = QWidget()
    lay = QVBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)
    value_label = QLabel(value)
    value_label.setStyleSheet("font-size: 26px; font-weight: bold;")
    value_label.setTextFormat(Qt.PlainText)
    caption = QLabel(label)
    caption.setObjectName("help-text")
    caption.setTextFormat(Qt.PlainText)
    lay.addWidget(value_label)
    lay.addWidget(caption)
    return box, value_label


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


def help_text(text: str, *, wrap: bool = True) -> QLabel:
    """Small, dimmed explanatory copy.

    Use for a single short line. For anything longer, prefer
    `collapsible_help()` — permanently-expanded paragraphs were the
    single biggest contributor to how much these pages scrolled.
    """
    label = QLabel(text)
    label.setObjectName("help-text")
    label.setWordWrap(wrap)
    label.setTextFormat(Qt.PlainText)
    return label


def collapsible_help(text: str, *, label: str = "") -> tuple[QWidget, QLabel]:
    """A '?' toggle plus the paragraph it shows/hides.

    Returns `(toggle_row, body)`. Add the toggle next to a section's
    title and the body directly beneath it; the body starts hidden, so
    the explanation is one click away instead of costing three or four
    lines of height on every visit.

    Long-form guidance is worth keeping — a headset mixer has plenty of
    non-obvious controls — but it shouldn't be the reason the real
    controls sit below the fold.
    """
    row = QWidget()
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(6)
    if label:
        lay.addWidget(section_title(label))
    button = QPushButton("?")
    button.setObjectName("help-toggle")
    button.setCheckable(True)
    button.setCursor(Qt.PointingHandCursor)
    show_text = QCoreApplication.translate("widgets", "Show explanation")
    hide_text = QCoreApplication.translate("widgets", "Hide explanation")
    button.setToolTip(show_text)
    button.setAccessibleName(
        QCoreApplication.translate("widgets", "Explanation for {section}").format(
            section=label
        ) if label else show_text
    )
    button.setAccessibleDescription(QCoreApplication.translate("widgets", "Collapsed"))
    lay.addWidget(button)
    lay.addStretch(1)

    body = QLabel(text)
    body.setObjectName("help-text")
    body.setWordWrap(True)
    body.setTextFormat(Qt.PlainText)
    body.setVisible(False)
    def set_expanded(expanded: bool) -> None:
        body.setVisible(expanded)
        button.setToolTip(hide_text if expanded else show_text)
        button.setAccessibleDescription(
            QCoreApplication.translate("widgets", "Expanded") if expanded
            else QCoreApplication.translate("widgets", "Collapsed")
        )

    button.toggled.connect(set_expanded)
    return row, body


def disclosure(
    title: str,
    *items: QWidget | QLayout,
    description: str = "",
    expanded: bool = False,
) -> QFrame:
    """Card disclosure for secondary controls, preserving child state."""
    frame = QFrame()
    frame.setObjectName("card")
    outer = QVBoxLayout(frame)
    outer.setContentsMargins(14, 12, 14, 12)
    outer.setSpacing(8)

    toggle = QPushButton(title)
    toggle.setCheckable(True)
    toggle.setChecked(expanded)
    toggle.setAccessibleName(title)
    toggle.setAccessibleDescription(
        QCoreApplication.translate("widgets", "Expanded") if expanded
        else QCoreApplication.translate("widgets", "Collapsed")
    )
    toggle.setStyleSheet("text-align: left; font-weight: bold;")
    outer.addWidget(toggle)

    body = QWidget()
    body_layout = QVBoxLayout(body)
    body_layout.setContentsMargins(0, 4, 0, 0)
    body_layout.setSpacing(10)
    if description:
        body_layout.addWidget(help_text(description))
    for item in items:
        if isinstance(item, QLayout):
            body_layout.addLayout(item)
        else:
            body_layout.addWidget(item)
    body.setVisible(expanded)

    def set_expanded(on: bool) -> None:
        body.setVisible(on)
        toggle.setAccessibleDescription(
            QCoreApplication.translate("widgets", "Expanded") if on
            else QCoreApplication.translate("widgets", "Collapsed")
        )

    toggle.toggled.connect(set_expanded)
    outer.addWidget(body)
    frame.disclosure_toggle = toggle
    frame.disclosure_body = body
    return frame


def notice(text: str, *, action: QWidget | None = None) -> QFrame:
    """Inline advisory strip — missing plugin, alpha feature, caveat.

    These used to be loose orange text plus a button floating in the
    card, which read as an error even when purely informational. A
    bordered band separates advisory content from the controls without
    shouting.
    """
    frame = QFrame()
    frame.setObjectName("notice")
    lay = QHBoxLayout(frame)
    lay.setContentsMargins(10, 7, 10, 7)
    lay.setSpacing(10)
    body = QLabel(text)
    body.setObjectName("notice-text")
    body.setWordWrap(True)
    body.setTextFormat(Qt.PlainText)
    lay.addWidget(body, 1)
    if action is not None:
        lay.addWidget(action, 0, Qt.AlignVCenter)
    return frame


def reflow_into_columns(
    box: QVBoxLayout, *, columns: int = 2, keep_first: int = 0
) -> None:
    """Re-lay a finished stack of cards into balanced columns, in place.

    Pages build themselves as one tall QVBoxLayout of cards, which on a
    1180px-wide window meant every control stretched across the full
    width with dead space beside it, and pages that could have fit on
    one screen scrolled instead. Rather than restructure each tab's
    construction, this takes the assembled stack apart and rebuilds it
    as columns.

    Cards are distributed by running height rather than alternating, so
    a tall card on the left doesn't leave a ragged gap on the right.
    `keep_first` pins that many cards full-width at the top, for pages
    that open with a banner or summary that shouldn't be halved.
    """
    items: list[QWidget] = []
    for i in reversed(range(box.count())):
        item = box.itemAt(i)
        widget = item.widget() if item else None
        if widget is not None:
            items.insert(0, widget)
            box.takeAt(i)
        else:
            # Stretches and spacers are re-created below; drop them.
            box.takeAt(i)

    if len([w for w in items if w.objectName() == "card"]) <= 1:
        for w in items:
            box.addWidget(w)
        box.addStretch(1)
        return

    for w in items[:keep_first]:
        box.addWidget(w)

    # Only cards get columnised. A page may also end with a bare
    # footnote label; dropping that into a grid cell leaves it floating
    # beside a card looking like it belongs to it, so trailing
    # non-cards are re-added full-width below the grid instead.
    rest = [w for w in items[keep_first:] if w.objectName() == "card"]
    trailing = [w for w in items[keep_first:] if w.objectName() != "card"]
    grid = QGridLayout()
    grid.setContentsMargins(0, 0, 0, 0)
    grid.setHorizontalSpacing(12)
    grid.setVerticalSpacing(12)
    # Greedy shortest-column packing using each card's preferred height.
    heights = [0] * columns
    rows = [0] * columns
    for widget in rest:
        col = heights.index(min(heights))
        # AlignTop matters: grid rows share a height, so without it a
        # tall card on one side stretches its neighbour and the
        # neighbour's contents drift apart vertically.
        grid.addWidget(widget, rows[col], col, Qt.AlignTop)
        rows[col] += 1
        heights[col] += max(widget.sizeHint().height(), 1)
    for col in range(columns):
        grid.setColumnStretch(col, 1)
    box.addLayout(grid)
    for w in trailing:
        box.addWidget(w)
    box.addStretch(1)


def column_grid(cards: list[QWidget], *, columns: int = 2) -> QGridLayout:
    """Lay independent cards out in columns instead of one tall stack.

    The window is ~1180px wide but every page was a single column, so
    each card stretched its controls across the full width with dead
    space on the right, and pages that could have fit on one screen
    scrolled instead. Only use this where the cards are genuinely
    independent — anything with a reading order stays stacked.
    """
    grid = QGridLayout()
    grid.setContentsMargins(0, 0, 0, 0)
    grid.setHorizontalSpacing(12)
    grid.setVerticalSpacing(12)
    for index, widget in enumerate(cards):
        grid.addWidget(widget, index // columns, index % columns)
    for col in range(columns):
        grid.setColumnStretch(col, 1)
    return grid


def app_icon() -> QIcon:
    """Return our installed icon, falling back to the generic theme icon
    when running from a source checkout that hasn't been installed yet."""
    return QIcon.fromTheme(APP_ICON, QIcon.fromTheme(APP_ICON_FALLBACK))


def _palette_tinted_icon(source: QIcon, normal: QColor) -> QIcon:
    """Return deterministic Normal/Selected variants for a theme icon.

    Kept separate from theme lookup so its contrast contract can be tested
    with a synthetic icon on runners that do not install Breeze/Adwaita.
    """
    def tinted(colour: QColor) -> QPixmap:
        pm = source.pixmap(QSize(22, 22))
        out = QPixmap(pm.size())
        out.fill(Qt.transparent)
        painter = QPainter(out)
        painter.drawPixmap(0, 0, pm)
        painter.setCompositionMode(QPainter.CompositionMode_SourceIn)
        painter.fillRect(out.rect(), colour)
        painter.end()
        return out

    icon = QIcon()
    icon.addPixmap(tinted(normal), QIcon.Normal, QIcon.Off)
    icon.addPixmap(tinted(QColor(ON_ACCENT)), QIcon.Selected, QIcon.Off)
    icon.addPixmap(tinted(normal), QIcon.Active, QIcon.Off)
    return icon


def themed_icon(*names: str) -> QIcon:
    """First icon-theme name that resolves, else a drawn placeholder.

    The sidebar used emoji glyphs as its icons. Emoji depend on an
    emoji font being installed and on that font's style matching the
    desktop, so they range from "fine" to literal tofu boxes, and they
    never match the surrounding icon set. Real theme icons pick up
    Breeze on KDE (our target) and Adwaita elsewhere, so the nav looks
    native instead of like a web page.

    The fallback is a small accent dot rather than nothing, so a
    missing icon degrades to something deliberate.
    """
    for name in names:
        source = QIcon.fromTheme(name)
        if not source.isNull() and source.availableSizes():
            app = QApplication.instance()
            normal = (
                app.palette().color(QPalette.Text)
                if app is not None else QColor("#202124")
            )
            return _palette_tinted_icon(source, normal)
    pm = QPixmap(20, 20)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(ACCENT))
    painter.drawEllipse(6, 6, 8, 8)
    painter.end()
    return QIcon(pm)


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
        if self.hasFocus():
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(ACCENT), 2))
            p.drawRoundedRect(QRectF(1, 1, w - 2, h - 2), radius, radius)


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


# Stylesheet for exclusive-mode picker buttons. QPushButton's default
# :checked state is visually identical to unchecked, so we paint the
# active mode with the app accent.
# Segmented mode picker (ANC mode, wireless mode, mic gain).
# Uses ACCENT rather than palette(highlight): the selected segment sits
# inches away from toggles and sliders that are all our green, and
# picking up the desktop highlight colour instead made the deck page
# look like it belonged to a different application.
MODE_BUTTON_STYLE = f"""
QPushButton {{
    padding: 6px 10px;
    border: 1px solid palette(mid);
    border-radius: 6px;
    background: palette(button);
}}
QPushButton:hover {{
    border-color: {ACCENT};
}}
QPushButton:checked {{
    background: {ACCENT};
    color: {ON_ACCENT};
    border-color: {ACCENT};
    font-weight: bold;
}}
QPushButton:disabled {{
    color: palette(placeholder-text);
    border-color: palette(midlight);
}}
"""


def mode_picker(
    parent: QObject,
    options: Iterable[tuple[str, str]],
    initial: str,
    on_select: Callable[[str], None],
    enabled: bool = True,
) -> tuple[QHBoxLayout, dict[str, QPushButton], QButtonGroup]:
    """Build an exclusive button-group picker (e.g. Off / Transparent /
    On for ANC). Returns the row layout, a key→button dict for echo
    handlers, and the QButtonGroup (caller stashes a reference so it
    isn't GC'd). All buttons share MODE_BUTTON_STYLE so the active
    one is visually distinct."""
    row = QHBoxLayout()
    row.setSpacing(6)
    group = QButtonGroup(parent)
    group.setExclusive(True)
    buttons: dict[str, QPushButton] = {}
    for key, label in options:
        btn = QPushButton(label)
        btn.setCheckable(True)
        btn.setEnabled(enabled)
        btn.setStyleSheet(MODE_BUTTON_STYLE)
        btn.clicked.connect(lambda _checked, k=key: on_select(k))
        buttons[key] = btn
        group.addButton(btn)
        row.addWidget(btn, 1)
    if initial in buttons:
        buttons[initial].setChecked(True)
    return row, buttons, group


def bind_debounced_slider(
    parent: QObject,
    slider: QSlider,
    value_label: QLabel,
    label_format: Callable[[int], str],
    send_fn: Callable[[int], None],
    interval_ms: int = 120,
) -> QTimer:
    """Wire a slider to a debounced send. The label updates instantly
    on every value change; the send callback fires `interval_ms` after
    the last change. Returns the timer so the caller can stash a
    reference (otherwise Qt GC's it the moment this returns)."""
    timer = QTimer(parent)
    timer.setSingleShot(True)
    timer.setInterval(interval_ms)

    def on_change(value: int) -> None:
        value_label.setText(label_format(int(value)))
        timer.start()

    def on_fire() -> None:
        send_fn(int(slider.value()))

    slider.valueChanged.connect(on_change)
    timer.timeout.connect(on_fire)
    return timer


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
    toggle.setAccessibleName(text)
    label.setBuddy(toggle)
    if tooltip:
        toggle.setToolTip(tooltip)
    row.addWidget(toggle, 0, alignment=Qt.AlignVCenter)
    return row, toggle
