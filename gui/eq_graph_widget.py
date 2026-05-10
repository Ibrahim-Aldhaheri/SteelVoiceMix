"""Sonar-style alternative EQ view.

Polished graph view of the 10-band parametric EQ. Click empty space
to drop a point, drag to shape it, right-click to remove. Hover
anywhere to read the freq + dB under the cursor. Mirrors the look
and feel of SteelSeries Sonar's EQ tab.

Performance:
  - Static background (gradient, zones, grid, axis labels) is
    rendered into a QPixmap on resize and blitted unchanged on every
    paint. Antialiased fills, gradient brushes and text layout —
    expensive in QPainter — only re-execute when geometry changes.
  - Frequency response is cached as a list of plot-space points.
    Invalidated only when bands or zones change; rebuilt lazily on
    the next paint. Hover repaints reuse the cache, so moving the
    cursor doesn't trigger any biquad math.
  - Per-band biquad coefficients are computed once per recompute and
    reused across every X sample (was once per X × band before, hot
    enough to cause drag lag in pure Python).

Deferred (saved in project memory): per-game zone-label dictionary,
Bass / Voice / Treble global sliders below the graph, right-click
filter-type / Q editor, reset-to-loaded-preset button.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QDateTime, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QLabel,
    QSizePolicy,
    QWidget,
)


FREQ_MIN_HZ = 20.0
FREQ_MAX_HZ = 20000.0
GAIN_MIN_DB = -12.0
GAIN_MAX_DB = 12.0

# fs has to match the daemon's filter chain (PipeWire's default
# 48 kHz) so the drawn curve doesn't deviate from the audible
# response near Nyquist.
SAMPLE_RATE_HZ = 48000.0
RESPONSE_SAMPLES = 320

DOT_RADIUS_PX = 11
DOT_HIT_RADIUS_PX = 18

# A band renders a dot iff its gain magnitude is at least this much.
# Below the floor it's invisible — the curve is flat there too, so
# there's nothing to anchor a dot to.
VISIBILITY_EPS_DB = 0.05

# Click on the 0 dB line snaps the placed dot to ±0.5 dB so it stays
# visible after release. Without the floor, the dot would disappear
# the moment the user lets go.
PLACEMENT_FLOOR_DB = 0.5

DEFAULT_ZONES: list[tuple[str, float, float]] = [
    ("SUB BASS", 20.0, 60.0),
    ("BASS", 60.0, 250.0),
    ("LOW MIDS", 250.0, 500.0),
    ("MID RANGE", 500.0, 2000.0),
    ("UPPER MIDS", 2000.0, 6000.0),
    ("HIGHS", 6000.0, 20000.0),
]

# (core, halo) per band slot. Core paints the dot's solid centre;
# halo paints the wider radial bloom underneath. Desaturated palette
# tuned to sit on the navy → black backdrop without competing with
# the mint-teal curve. Each color is roughly 60 % saturation / 75 %
# brightness — distinguishable enough to cross-reference with the
# slider grid, harmonious enough that 10 dots clustered don't look
# like a fruit salad.
DOT_PALETTE: list[tuple[str, str]] = [
    ("#D88B9A", "#D88B9A40"),  # 1 — dusty rose
    ("#D9A077", "#D9A07740"),  # 2 — muted apricot
    ("#D4BD7A", "#D4BD7A40"),  # 3 — sand
    ("#A8C68B", "#A8C68B40"),  # 4 — sage
    ("#7CC8B5", "#7CC8B540"),  # 5 — soft mint (sibling of the curve)
    ("#88AED4", "#88AED440"),  # 6 — periwinkle
    ("#A599D4", "#A599D440"),  # 7 — lavender
    ("#C490C0", "#C490C040"),  # 8 — orchid
    ("#D49BB0", "#D49BB040"),  # 9 — dusty pink
    ("#9AAAB8", "#9AAAB840"),  # 10 — slate blue
]

CURVE_COLOR = "#50E3C2"
CURVE_BELOW_COLOR = "#FF7E8A"
BG_TOP = "#1A1B2E"
BG_BOTTOM = "#0B0C18"
ZONE_BG_A = "#22223A"
ZONE_BG_B = "#2C2C46"
ZONE_TEXT = "#B5B5D8"
GRID_MINOR = QColor(255, 255, 255, 18)
GRID_MAJOR = QColor(255, 255, 255, 55)
AXIS_TEXT = "#8A8FAE"
HOVER_LINE = QColor(255, 255, 255, 80)
EMPTY_HINT_COLOR = "#8A8FAE"
TOOLTIP_BG = QColor(20, 22, 38, 230)
TOOLTIP_TEXT = "#E0E5FF"


def _hz_to_x(hz: float, plot_left: float, plot_right: float) -> float:
    lo = math.log10(FREQ_MIN_HZ)
    hi = math.log10(FREQ_MAX_HZ)
    lhz = math.log10(max(hz, FREQ_MIN_HZ))
    return plot_left + (lhz - lo) / (hi - lo) * (plot_right - plot_left)


def _x_to_hz(x: float, plot_left: float, plot_right: float) -> float:
    if plot_right <= plot_left:
        return FREQ_MIN_HZ
    frac = max(0.0, min(1.0, (x - plot_left) / (plot_right - plot_left)))
    lo = math.log10(FREQ_MIN_HZ)
    hi = math.log10(FREQ_MAX_HZ)
    return 10.0 ** (lo + frac * (hi - lo))


def _db_to_y(db: float, plot_top: float, plot_bottom: float) -> float:
    db = max(GAIN_MIN_DB, min(GAIN_MAX_DB, db))
    frac = (db - GAIN_MIN_DB) / (GAIN_MAX_DB - GAIN_MIN_DB)
    return plot_bottom - frac * (plot_bottom - plot_top)


def _y_to_db(y: float, plot_top: float, plot_bottom: float) -> float:
    if plot_bottom <= plot_top:
        return 0.0
    frac = max(0.0, min(1.0, (plot_bottom - y) / (plot_bottom - plot_top)))
    return GAIN_MIN_DB + frac * (GAIN_MAX_DB - GAIN_MIN_DB)


def _format_freq_label(hz: float) -> str:
    if hz < 1000:
        return f"{int(round(hz))} Hz"
    khz = hz / 1000.0
    if abs(khz - round(khz)) < 0.05:
        return f"{int(round(khz))} kHz"
    return f"{khz:.2f} kHz".rstrip("0").rstrip(".")


def _biquad_coeffs(
    freq_hz: float, q: float, gain_db: float, ftype: str,
) -> tuple[float, float, float, float, float, float]:
    """RBJ cookbook biquad coefficients. Same math as PipeWire's
    builtin biquads so what's drawn matches what's audible."""
    A = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * math.pi * freq_hz / SAMPLE_RATE_HZ
    cw = math.cos(w0)
    sw = math.sin(w0)
    alpha = sw / (2.0 * max(q, 0.01))
    if ftype == "peaking":
        return (
            1.0 + alpha * A, -2.0 * cw, 1.0 - alpha * A,
            1.0 + alpha / A, -2.0 * cw, 1.0 - alpha / A,
        )
    sqA = math.sqrt(A)
    if ftype == "lowshelf":
        return (
            A * ((A + 1) - (A - 1) * cw + 2.0 * sqA * alpha),
            2.0 * A * ((A - 1) - (A + 1) * cw),
            A * ((A + 1) - (A - 1) * cw - 2.0 * sqA * alpha),
            (A + 1) + (A - 1) * cw + 2.0 * sqA * alpha,
            -2.0 * ((A - 1) + (A + 1) * cw),
            (A + 1) + (A - 1) * cw - 2.0 * sqA * alpha,
        )
    return (
        A * ((A + 1) + (A - 1) * cw + 2.0 * sqA * alpha),
        -2.0 * A * ((A - 1) + (A + 1) * cw),
        A * ((A + 1) + (A - 1) * cw - 2.0 * sqA * alpha),
        (A + 1) - (A - 1) * cw + 2.0 * sqA * alpha,
        2.0 * ((A - 1) - (A + 1) * cw),
        (A + 1) - (A - 1) * cw - 2.0 * sqA * alpha,
    )


def _summed_response_db(
    coeffs_per_band: list[tuple[float, float, float, float, float, float]],
    freq_hz: float,
) -> float:
    """Total magnitude (dB) of the cascaded biquads at one freq.
    Pulled out of the per-X sample loop so the only per-X work is
    the four trig calls + the band sum, not the coefficient
    computation."""
    w = 2.0 * math.pi * freq_hz / SAMPLE_RATE_HZ
    cw1, sw1 = math.cos(-w), math.sin(-w)
    cw2, sw2 = math.cos(-2.0 * w), math.sin(-2.0 * w)
    total = 0.0
    for b0, b1, b2, a0, a1, a2 in coeffs_per_band:
        nr = b0 + b1 * cw1 + b2 * cw2
        ni = b1 * sw1 + b2 * sw2
        dr = a0 + a1 * cw1 + a2 * cw2
        di = a1 * sw1 + a2 * sw2
        num_mag = math.hypot(nr, ni)
        den_mag = math.hypot(dr, di)
        if den_mag <= 0:
            continue
        total += 20.0 * math.log10(max(num_mag / den_mag, 1e-9))
    return total


# Filter types we expose in the inspector. The daemon supports more
# (lowpass / highpass / bandpass / notch / allpass via PipeWire's
# builtin biquads) but most aren't useful in a mastering-EQ context;
# mirroring ASM's set keeps the user-facing palette focused.
_INSPECTOR_FILTER_TYPES: list[tuple[str, str]] = [
    ("peaking",   "Peaking"),
    ("lowshelf",  "Low shelf"),
    ("highshelf", "High shelf"),
    ("lowpass",   "Low pass"),
    ("highpass",  "High pass"),
]


class EqBandInspector(QFrame):
    """Floating popup showing the selected band's parameters with
    bidirectional editing. Lives as a child of EqGraphWidget so it
    floats above the curve; positions itself next to the selected
    dot, flipping side when there isn't room.

    Signal:
      band_edited(int): the band dict in the parent's _bands has
        already been updated with the new value(s); the parent
        flushes a set-eq-band commit. Emitted on any field commit
        (combo change, spinbox editing-finished)."""

    band_edited = Signal(int)

    def __init__(self, graph: "EqGraphWidget") -> None:
        super().__init__(graph)
        self._graph = graph
        self._idx: int = -1
        self._loading = False
        self.setObjectName("eq_band_inspector")
        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet(
            "QFrame#eq_band_inspector {"
            "  background-color: rgba(20, 22, 38, 235);"
            "  border: 1px solid rgba(255,255,255,60);"
            "  border-radius: 7px;"
            "}"
            "QLabel { color: #B5B5D8; background: transparent; "
            "  border: none; font-size: 9pt; }"
            "QLabel#title { color: #FFFFFF; font-weight: bold; "
            "  font-size: 10pt; }"
            "QComboBox, QDoubleSpinBox {"
            "  background: #2C2C46; color: #E0E5FF;"
            "  border: 1px solid rgba(255,255,255,40);"
            "  border-radius: 4px; padding: 2px 6px;"
            "  font-size: 9pt; min-width: 70px;"
            "}"
            "QComboBox::drop-down { border: none; }"
        )
        grid = QGridLayout(self)
        grid.setContentsMargins(10, 8, 10, 10)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(5)

        self._title = QLabel("Band")
        self._title.setObjectName("title")
        grid.addWidget(self._title, 0, 0, 1, 2)

        self._type = QComboBox()
        for key, label in _INSPECTOR_FILTER_TYPES:
            self._type.addItem(self.tr(label), key)
        self._type.currentIndexChanged.connect(self._on_type_changed)
        grid.addWidget(QLabel(self.tr("Type")), 1, 0)
        grid.addWidget(self._type, 1, 1)

        self._gain = QDoubleSpinBox()
        self._gain.setRange(GAIN_MIN_DB, GAIN_MAX_DB)
        self._gain.setSuffix(" dB")
        self._gain.setSingleStep(0.5)
        self._gain.setDecimals(1)
        self._gain.editingFinished.connect(self._on_gain_changed)
        grid.addWidget(QLabel(self.tr("Gain")), 2, 0)
        grid.addWidget(self._gain, 2, 1)

        self._freq = QDoubleSpinBox()
        self._freq.setRange(FREQ_MIN_HZ, FREQ_MAX_HZ)
        self._freq.setSuffix(" Hz")
        self._freq.setSingleStep(10.0)
        self._freq.setDecimals(0)
        self._freq.editingFinished.connect(self._on_freq_changed)
        grid.addWidget(QLabel(self.tr("Freq")), 3, 0)
        grid.addWidget(self._freq, 3, 1)

        self._q = QDoubleSpinBox()
        self._q.setRange(EqGraphWidget.Q_MIN, EqGraphWidget.Q_MAX)
        self._q.setSingleStep(0.1)
        self._q.setDecimals(2)
        self._q.editingFinished.connect(self._on_q_changed)
        grid.addWidget(QLabel(self.tr("Q")), 4, 0)
        grid.addWidget(self._q, 4, 1)

        self.adjustSize()
        self.hide()

    def show_for_band(self, idx: int) -> None:
        """Full show — sets the index, populates fields, repositions,
        and brings the popup to front. Use only on selection changes,
        not on per-drag-tick refreshes (which would call show() +
        raise_() at 60 Hz and cause visible flicker)."""
        if idx < 0 or idx >= len(self._graph._bands):
            self.hide()
            self._idx = -1
            return
        self._idx = idx
        self._sync_fields_from_band()
        self._reposition()
        self.show()
        self.raise_()

    def refresh_from_band(self) -> None:
        """Lightweight tick — pulls current values into the spinners
        and repositions, but doesn't touch show()/raise_(). Cheap to
        call from mouseMoveEvent."""
        if self._idx < 0 or not self.isVisible():
            return
        self._sync_fields_from_band()
        self._reposition()

    def _sync_fields_from_band(self) -> None:
        if self._idx < 0 or self._idx >= len(self._graph._bands):
            return
        band = self._graph._bands[self._idx]
        self._loading = True
        try:
            self._title.setText(self.tr("Band {n}").format(n=self._idx + 1))
            ftype = str(band.get("type", "peaking"))
            type_idx = next(
                (i for i, (k, _) in enumerate(_INSPECTOR_FILTER_TYPES) if k == ftype),
                0,
            )
            self._type.setCurrentIndex(type_idx)
            self._gain.setValue(float(band.get("gain", 0.0)))
            self._freq.setValue(float(band.get("freq", 1000.0)))
            self._q.setValue(float(band.get("q", 1.0)))
        finally:
            self._loading = False

    def _reposition(self) -> None:
        if self._idx < 0 or self._idx >= len(self._graph._bands):
            return
        band = self._graph._bands[self._idx]
        dot = self._graph._band_dot_pos(band)
        self.adjustSize()
        iw, ih = self.width(), self.height()
        gw, gh = self._graph.width(), self._graph.height()
        # Default: place to the right of the dot; flip left if it'd
        # overflow. Vertically centered on the dot, clamped to the
        # widget bounds.
        x = int(dot.x() + DOT_RADIUS_PX + 12)
        if x + iw > gw - 4:
            x = int(dot.x() - DOT_RADIUS_PX - 12 - iw)
        x = max(4, min(x, gw - iw - 4))
        y = int(dot.y() - ih / 2)
        y = max(4, min(y, gh - ih - 4))
        self.move(x, y)

    def _on_type_changed(self, _idx: int) -> None:
        if self._loading or self._idx < 0:
            return
        new_type = self._type.currentData()
        band = self._graph._bands[self._idx]
        if str(band.get("type", "peaking")) == new_type:
            return
        band["type"] = new_type
        self._graph._curve_points = None
        self._graph.update()
        self.band_edited.emit(self._idx)

    def _on_gain_changed(self) -> None:
        if self._loading or self._idx < 0:
            return
        band = self._graph._bands[self._idx]
        new_gain = float(self._gain.value())
        if abs(float(band.get("gain", 0.0)) - new_gain) < 1e-3:
            return
        band["gain"] = new_gain
        self._graph._curve_points = None
        self._graph.update()
        self._reposition()
        self.band_edited.emit(self._idx)

    def _on_freq_changed(self) -> None:
        if self._loading or self._idx < 0:
            return
        band = self._graph._bands[self._idx]
        new_freq = float(self._freq.value())
        if abs(float(band.get("freq", 1000.0)) - new_freq) < 1e-3:
            return
        band["freq"] = new_freq
        self._graph._curve_points = None
        self._graph.update()
        self._reposition()
        self.band_edited.emit(self._idx)

    def _on_q_changed(self) -> None:
        if self._loading or self._idx < 0:
            return
        band = self._graph._bands[self._idx]
        new_q = float(self._q.value())
        if abs(float(band.get("q", 1.0)) - new_q) < 1e-3:
            return
        band["q"] = new_q
        self._graph._curve_points = None
        self._graph.update()
        self.band_edited.emit(self._idx)


class EqGraphWidget(QWidget):
    """Sonar-style EQ view: dot-on-a-curve graph with zone-label header.

    Signals:
      bandChanged(int, float, float):
        Drag tick. Args: (band_index 0..N-1, freq_hz, gain_db).
      bandReleased(int):
        Drag finished — owner flushes pending commits.
    """

    bandChanged = Signal(int, float, float)
    bandQChanged = Signal(int, float)        # band_idx, q
    bandReleased = Signal(int)
    selectionChanged = Signal(int)           # -1 when nothing selected

    PLOT_PAD_LEFT = 42
    PLOT_PAD_RIGHT = 16
    PLOT_PAD_TOP = 32
    PLOT_PAD_BOTTOM = 30

    # Q range for scroll-wheel adjustment. Tracks ASM's range so
    # the user-perceived sharpness ceiling matches.
    Q_MIN = 0.1
    Q_MAX = 10.0
    Q_STEP_PER_NOTCH = 0.1

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._bands: list[dict] = []
        self._zones: list[tuple[str, float, float]] = list(DEFAULT_ZONES)
        self._dragging_band: int | None = None
        self._drag_offset: tuple[float, float] = (0.0, 0.0)
        self._hover_pos: QPointF | None = None
        self._bg_pixmap: QPixmap | None = None
        self._curve_points: list[QPointF] | None = None
        self._selected_band: int = -1
        # Banner shown briefly when a placement click lands on a
        # full graph (every slot already in use). Cleared by a
        # short timer so the user knows what happened without
        # cluttering steady-state.
        self._slot_full_banner_until_ms: int = 0
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumHeight(320)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        # Floating inspector — shows the selected band's params with
        # bidirectional editing. Owned by us; the parent tab connects
        # to its band_edited signal to push set-eq-band commits.
        self.band_inspector = EqBandInspector(self)
        self.selectionChanged.connect(self._on_selection_changed_internal)

    def set_bands(self, bands: list[dict]) -> None:
        """Replace the bands shown. Stores shallow copies. Skipped
        while a drag is in progress so daemon echoes don't yank the
        in-flight dot out from under the cursor."""
        if self._dragging_band is not None:
            return
        self._bands = [dict(b) for b in bands]
        self._curve_points = None
        self.update()

    def set_zones(self, zones: list[tuple[str, float, float]] | None) -> None:
        self._zones = list(zones) if zones else list(DEFAULT_ZONES)
        self._bg_pixmap = None
        self.update()

    def _plot_rect(self) -> QRectF:
        return QRectF(
            self.PLOT_PAD_LEFT,
            self.PLOT_PAD_TOP,
            max(1.0, self.width() - self.PLOT_PAD_LEFT - self.PLOT_PAD_RIGHT),
            max(1.0, self.height() - self.PLOT_PAD_TOP - self.PLOT_PAD_BOTTOM),
        )

    def _band_dot_pos(self, band: dict) -> QPointF:
        r = self._plot_rect()
        x = _hz_to_x(float(band.get("freq", 1000.0)), r.left(), r.right())
        y = _db_to_y(float(band.get("gain", 0.0)), r.top(), r.bottom())
        return QPointF(x, y)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._bg_pixmap = None
        self._curve_points = None
        if self._selected_band >= 0:
            self.band_inspector.refresh_from_band()
        self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.LeftButton or not self._bands:
            return
        pos = event.position()
        rect = self._plot_rect()
        if not rect.contains(pos):
            return
        best_idx = self._dot_at(pos)
        if best_idx < 0:
            best_idx = self._pick_placement_slot(pos.x())
            if best_idx < 0:
                # Every slot's in use — flash a briefly-visible
                # banner explaining the click was a no-op. Tied to
                # wall-clock so a stale banner can't linger across
                # repaints; a 2.4 s single-shot timer schedules the
                # repaint that hides it.
                self._slot_full_banner_until_ms = (
                    QDateTime.currentMSecsSinceEpoch() + 2200
                )
                QTimer.singleShot(2400, self.update)
                self.update()
                return
            new_freq, new_gain = self._click_to_band_coords(pos)
            band = self._bands[best_idx]
            ftype = str(band.get("type", "peaking"))
            if ftype not in ("lowshelf", "highshelf"):
                band["freq"] = new_freq
            band["gain"] = new_gain
            self._curve_points = None
            self.bandChanged.emit(best_idx, float(band["freq"]), new_gain)
        self._set_selected(best_idx)
        self._dragging_band = best_idx
        dot = self._band_dot_pos(self._bands[best_idx])
        self._drag_offset = (pos.x() - dot.x(), pos.y() - dot.y())
        self.setCursor(Qt.ClosedHandCursor)
        self.update()
        event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        """Double-click a dot to clear it (gain → 0). Mirrors ASM —
        primary "remove" gesture instead of right-click."""
        if event.button() != Qt.LeftButton or not self._bands:
            return
        pos = event.position()
        idx = self._dot_at(pos)
        if idx < 0:
            return
        band = self._bands[idx]
        band["gain"] = 0.0
        self._curve_points = None
        if self._selected_band == idx:
            self._set_selected(-1)
        self.bandChanged.emit(idx, float(band.get("freq", 1000.0)), 0.0)
        self.bandReleased.emit(idx)
        self.update()
        event.accept()

    def wheelEvent(self, event) -> None:
        """Scroll wheel on the selected band → adjust Q. No-op if no
        band is currently selected (can't pick blindly — Q changes
        the curve shape and the user needs visual confirmation of
        which band they're shaping)."""
        idx = self._selected_band
        if idx < 0 or not (0 <= idx < len(self._bands)):
            event.ignore()
            return
        band = self._bands[idx]
        # angleDelta is 120 per notch on most mice (Qt convention).
        notches = event.angleDelta().y() / 120.0
        if notches == 0:
            return
        q = float(band.get("q", 1.0)) + notches * self.Q_STEP_PER_NOTCH
        q = max(self.Q_MIN, min(self.Q_MAX, round(q, 3)))
        if abs(q - float(band.get("q", 1.0))) < 1e-4:
            return
        band["q"] = q
        self._curve_points = None
        self.bandQChanged.emit(idx, q)
        self.bandReleased.emit(idx)
        self.update()
        event.accept()

    def _set_selected(self, idx: int) -> None:
        if idx == self._selected_band:
            return
        self._selected_band = idx
        self.selectionChanged.emit(idx)

    def selected_band(self) -> int:
        return self._selected_band

    def _on_selection_changed_internal(self, idx: int) -> None:
        if idx < 0:
            self.band_inspector.hide()
        else:
            self.band_inspector.show_for_band(idx)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        self._hover_pos = event.position()
        if self._dragging_band is None:
            self._update_hover_cursor(event.position())
            self.update()
            return
        idx = self._dragging_band
        if not (0 <= idx < len(self._bands)):
            return
        band = self._bands[idx]
        r = self._plot_rect()
        x = event.position().x() - self._drag_offset[0]
        y = event.position().y() - self._drag_offset[1]
        new_freq = _x_to_hz(x, r.left(), r.right())
        new_gain = _y_to_db(y, r.top(), r.bottom())
        ftype = str(band.get("type", "peaking"))
        if ftype in ("lowshelf", "highshelf"):
            new_freq = float(band.get("freq", new_freq))
        else:
            new_freq = max(FREQ_MIN_HZ * 1.1, min(FREQ_MAX_HZ * 0.9, new_freq))
        band["freq"] = new_freq
        band["gain"] = new_gain
        self._curve_points = None
        self.bandChanged.emit(idx, new_freq, new_gain)
        # Inspector spinners track the drag so the user can read the
        # in-flight values without releasing — and reposition follows
        # the dot.
        if self._selected_band == idx:
            self.band_inspector.refresh_from_band()
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.LeftButton or self._dragging_band is None:
            return
        idx = self._dragging_band
        self._dragging_band = None
        self.unsetCursor()
        self.bandReleased.emit(idx)
        self.update()

    def leaveEvent(self, _event) -> None:
        self._hover_pos = None
        self.update()

    def contextMenuEvent(self, event) -> None:
        """Right-click on a dot → reset that band's gain to 0, hiding
        the dot and freeing the slot for a new placement."""
        if not self._bands:
            return
        pos = QPointF(event.pos().x(), event.pos().y())
        for idx, band in enumerate(self._bands):
            if not self._band_is_visible(band):
                continue
            dot = self._band_dot_pos(band)
            if math.hypot(pos.x() - dot.x(), pos.y() - dot.y()) <= DOT_HIT_RADIUS_PX:
                band["gain"] = 0.0
                self._curve_points = None
                if self._selected_band == idx:
                    self._set_selected(-1)
                self.bandChanged.emit(idx, float(band.get("freq", 1000.0)), 0.0)
                self.bandReleased.emit(idx)
                self.update()
                event.accept()
                return

    def _dot_at(self, pos: QPointF) -> int:
        best_idx = -1
        best_dist = float("inf")
        for idx, band in enumerate(self._bands):
            if not self._band_is_visible(band):
                continue
            dot = self._band_dot_pos(band)
            d = math.hypot(pos.x() - dot.x(), pos.y() - dot.y())
            if d <= DOT_HIT_RADIUS_PX and d < best_dist:
                best_dist = d
                best_idx = idx
        return best_idx

    def _band_is_visible(self, band: dict) -> bool:
        if not band.get("enabled", True):
            return False
        return abs(float(band.get("gain", 0.0))) >= VISIBILITY_EPS_DB

    def _click_to_band_coords(self, pos: QPointF) -> tuple[float, float]:
        r = self._plot_rect()
        freq = _x_to_hz(pos.x(), r.left(), r.right())
        gain = _y_to_db(pos.y(), r.top(), r.bottom())
        if abs(gain) < PLACEMENT_FLOOR_DB:
            gain = PLACEMENT_FLOOR_DB if gain >= 0 else -PLACEMENT_FLOOR_DB
        return freq, gain

    def _pick_placement_slot(self, click_x: float) -> int:
        """Pick the unused slot whose default freq is closest (in
        log space) to the click. Prefers peaking; falls back to a
        shelf only if no peaking slots are free."""
        r = self._plot_rect()
        click_hz = _x_to_hz(click_x, r.left(), r.right())
        best_peak = -1
        best_peak_dist = float("inf")
        best_shelf = -1
        best_shelf_dist = float("inf")
        for idx, band in enumerate(self._bands):
            if self._band_is_visible(band):
                continue
            band_hz = float(band.get("freq", 1000.0))
            dist = abs(math.log10(max(band_hz, 1.0)) -
                       math.log10(max(click_hz, 1.0)))
            if str(band.get("type", "peaking")) == "peaking":
                if dist < best_peak_dist:
                    best_peak_dist, best_peak = dist, idx
            else:
                if dist < best_shelf_dist:
                    best_shelf_dist, best_shelf = dist, idx
        return best_peak if best_peak >= 0 else best_shelf

    def _update_hover_cursor(self, pos: QPointF) -> None:
        for band in self._bands:
            if not self._band_is_visible(band):
                continue
            dot = self._band_dot_pos(band)
            if math.hypot(pos.x() - dot.x(), pos.y() - dot.y()) <= DOT_HIT_RADIUS_PX:
                self.setCursor(Qt.OpenHandCursor)
                return
        if self._plot_rect().contains(pos):
            self.setCursor(Qt.CrossCursor)
        else:
            self.unsetCursor()

    def paintEvent(self, _event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        if self._bg_pixmap is None:
            self._bg_pixmap = self._build_static_background()
        p.drawPixmap(0, 0, self._bg_pixmap)

        rect = self._plot_rect()
        if self._curve_points is None:
            self._curve_points = self._compute_curve_points(rect)

        if any(self._band_is_visible(b) for b in self._bands):
            self._paint_curve(p, rect, self._curve_points)
        else:
            self._paint_empty_hint(p, rect)

        self._paint_dots(p, rect)
        if self._hover_pos and self._dragging_band is None:
            self._paint_hover_crosshair(p, rect, self._hover_pos)
        self._paint_slot_full_banner(p, rect)

    def _build_static_background(self) -> QPixmap:
        """Render the parts that don't change between paints — the
        gradient backdrop, zone strip, dB/Hz grid, axis labels, plot
        border — into a pixmap. Reused on every paint until the
        widget is resized or the zone list is replaced."""
        w = max(1, self.width())
        h = max(1, self.height())
        dpr = self.devicePixelRatioF()
        pix = QPixmap(int(w * dpr), int(h * dpr))
        pix.setDevicePixelRatio(dpr)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        bg_grad = QLinearGradient(0, 0, 0, h)
        bg_grad.setColorAt(0.0, QColor(BG_TOP))
        bg_grad.setColorAt(1.0, QColor(BG_BOTTOM))
        p.fillRect(0, 0, w, h, bg_grad)
        rect = self._plot_rect()
        self._paint_zones(p, rect)
        self._paint_grid(p, rect)
        p.end()
        return pix

    def _paint_zones(self, p: QPainter, rect: QRectF) -> None:
        strip_h = self.PLOT_PAD_TOP - 10
        strip_top = 5
        font = QFont(self.font())
        font.setPointSizeF(max(8.0, font.pointSizeF() - 0.5))
        font.setBold(True)
        font.setLetterSpacing(QFont.AbsoluteSpacing, 0.6)
        p.setFont(font)
        fm = QFontMetrics(font)
        zone_pen = QPen(QColor(ZONE_TEXT))
        for i, (label, f_lo, f_hi) in enumerate(self._zones):
            x_lo = _hz_to_x(f_lo, rect.left(), rect.right())
            x_hi = _hz_to_x(f_hi, rect.left(), rect.right())
            zone_rect = QRectF(x_lo, strip_top, max(1.0, x_hi - x_lo), strip_h)
            shade = QColor(ZONE_BG_A) if i % 2 == 0 else QColor(ZONE_BG_B)
            p.fillRect(zone_rect, shade)
            if i > 0:
                p.setPen(QPen(QColor(255, 255, 255, 24), 1))
                p.drawLine(QPointF(x_lo, strip_top + 2),
                           QPointF(x_lo, strip_top + strip_h - 2))
            p.setPen(zone_pen)
            text = label.upper()
            if fm.horizontalAdvance(text) > zone_rect.width() - 6:
                text = fm.elidedText(text, Qt.ElideRight, int(zone_rect.width() - 6))
            p.drawText(zone_rect, Qt.AlignCenter, text)

    def _paint_grid(self, p: QPainter, rect: QRectF) -> None:
        font = QFont(self.font())
        font.setPointSizeF(max(7.5, font.pointSizeF() - 1.5))
        p.setFont(font)
        for db in (-12, -6, 0, 6, 12):
            y = _db_to_y(float(db), rect.top(), rect.bottom())
            p.setPen(QPen(GRID_MAJOR if db == 0 else GRID_MINOR, 1))
            p.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
            p.setPen(QColor(AXIS_TEXT))
            label = "0" if db == 0 else f"{db:+d}"
            p.drawText(
                QRectF(0, y - 8, rect.left() - 6, 16),
                Qt.AlignRight | Qt.AlignVCenter,
                label,
            )
        # Frequency grid: 1/3-decade lines minor, decade lines major.
        for hz, label, major in (
            (30, "30", False), (100, "100", True), (300, "300", False),
            (1000, "1k", True), (3000, "3k", False), (10000, "10k", True),
        ):
            x = _hz_to_x(hz, rect.left(), rect.right())
            p.setPen(QPen(GRID_MAJOR if major else GRID_MINOR, 1))
            p.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
            p.setPen(QColor(AXIS_TEXT))
            p.drawText(
                QRectF(x - 24, rect.bottom() + 5, 48, 16),
                Qt.AlignCenter, label,
            )
        p.setPen(QPen(QColor(255, 255, 255, 50), 1.0))
        p.drawRoundedRect(rect, 5.0, 5.0)

    def _compute_curve_points(self, rect: QRectF) -> list[QPointF]:
        active: list[tuple[float, float, float, float, float, float]] = []
        for band in self._bands:
            if not band.get("enabled", True):
                continue
            gain = float(band.get("gain", 0.0))
            if abs(gain) < 1e-4:
                continue
            active.append(_biquad_coeffs(
                float(band.get("freq", 1000.0)),
                float(band.get("q", 1.0)),
                gain,
                str(band.get("type", "peaking")),
            ))
        if not active:
            zero_y = _db_to_y(0.0, rect.top(), rect.bottom())
            return [QPointF(rect.left(), zero_y), QPointF(rect.right(), zero_y)]
        n = RESPONSE_SAMPLES
        pts: list[QPointF] = []
        left = rect.left()
        width = rect.width()
        top = rect.top()
        bottom = rect.bottom()
        for i in range(n):
            frac = i / (n - 1)
            x = left + frac * width
            hz = _x_to_hz(x, left, left + width)
            db = _summed_response_db(active, hz)
            db = max(GAIN_MIN_DB, min(GAIN_MAX_DB, db))
            pts.append(QPointF(x, _db_to_y(db, top, bottom)))
        return pts

    def _paint_curve(self, p: QPainter, rect: QRectF, pts: list[QPointF]) -> None:
        if len(pts) < 2:
            return
        zero_y = _db_to_y(0.0, rect.top(), rect.bottom())
        path = QPainterPath()
        path.moveTo(pts[0])
        for pt in pts[1:]:
            path.lineTo(pt)
        # Filled area between the curve and the 0 dB line. Mint-teal
        # above zero (boost), warm pink below (cut). Reads as Sonar's
        # at-a-glance "where am I lifting / cutting" signal.
        fill = QPainterPath(path)
        fill.lineTo(pts[-1].x(), zero_y)
        fill.lineTo(pts[0].x(), zero_y)
        fill.closeSubpath()
        zero_frac = max(0.0, min(1.0,
            (zero_y - rect.top()) / max(1.0, rect.height())))
        fill_grad = QLinearGradient(0, rect.top(), 0, rect.bottom())
        c_above = QColor(CURVE_COLOR); c_above.setAlpha(135)
        c_zero = QColor(CURVE_COLOR); c_zero.setAlpha(0)
        c_below = QColor(CURVE_BELOW_COLOR); c_below.setAlpha(120)
        fill_grad.setColorAt(0.0, c_above)
        fill_grad.setColorAt(zero_frac, c_zero)
        fill_grad.setColorAt(min(1.0, zero_frac + 0.0001), c_zero)
        fill_grad.setColorAt(1.0, c_below)
        p.fillPath(fill, fill_grad)
        # Soft outer glow under the stroke so the curve reads as
        # luminous, not just a flat 1 px line.
        glow = QColor(CURVE_COLOR); glow.setAlpha(85)
        p.setPen(QPen(glow, 6.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawPath(path)
        # Crisp top stroke.
        p.setPen(QPen(QColor(CURVE_COLOR), 2.4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawPath(path)

    def _paint_empty_hint(self, p: QPainter, rect: QRectF) -> None:
        font = QFont(self.font())
        font.setPointSizeF(max(11.0, font.pointSizeF() + 1.0))
        font.setItalic(True)
        p.setFont(font)
        p.setPen(QColor(EMPTY_HINT_COLOR))
        p.drawText(rect, Qt.AlignCenter,
                   self.tr("Click anywhere on the graph to add a point"))

    def _paint_dots(self, p: QPainter, rect: QRectF) -> None:
        for idx, band in enumerate(self._bands):
            # Dragged band always paints, even if its gain is
            # momentarily passing through 0 dB during the drag.
            if not self._band_is_visible(band) and self._dragging_band != idx:
                continue
            dot = self._band_dot_pos(band)
            core_hex, halo_hex = DOT_PALETTE[idx % len(DOT_PALETTE)]
            is_selected = (self._selected_band == idx)
            is_dragging = (self._dragging_band == idx)
            highlight = is_selected or is_dragging
            r = DOT_RADIUS_PX + (1.5 if highlight else 0.0)
            if is_selected and not is_dragging:
                # Outer ring around the selected dot — telegraphs
                # "scroll wheel adjusts this band's Q" without text.
                ring_pen = QPen(QColor(255, 255, 255, 200), 1.6)
                p.setPen(ring_pen)
                p.setBrush(Qt.NoBrush)
                p.drawEllipse(dot, r + 5.0, r + 5.0)
            # Soft halo. Three stops so the bloom fades gracefully
            # rather than ending in a hard ring.
            halo_r = r * 2.4
            halo = QRadialGradient(dot, halo_r)
            halo.setColorAt(0.0, QColor(halo_hex))
            halo.setColorAt(0.5, QColor(halo_hex))
            halo.setColorAt(1.0, QColor(0, 0, 0, 0))
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(halo))
            p.drawEllipse(dot, halo_r, halo_r)
            # Core orb — radial gradient with the highlight offset
            # toward the upper-left to give it a 3D feel.
            core = QRadialGradient(
                QPointF(dot.x() - r * 0.3, dot.y() - r * 0.3),
                r * 1.4,
            )
            core.setColorAt(0.0, QColor(core_hex).lighter(135))
            core.setColorAt(1.0, QColor(core_hex).darker(120))
            p.setBrush(QBrush(core))
            ring_alpha = 240 if highlight else 90
            p.setPen(QPen(QColor(255, 255, 255, ring_alpha), 1.4))
            p.drawEllipse(dot, r, r)
            # Slot number — small, white, only as a quick reference
            # for cross-checking against the sliders view.
            p.setPen(QColor("#FFFFFF"))
            f = QFont(self.font())
            f.setPointSizeF(max(7.5, f.pointSizeF() - 1.0))
            f.setBold(True)
            p.setFont(f)
            p.drawText(QRectF(dot.x() - r, dot.y() - r, 2 * r, 2 * r),
                       Qt.AlignCenter, str(idx + 1))

    def _paint_hover_crosshair(
        self, p: QPainter, rect: QRectF, pos: QPointF,
    ) -> None:
        if not rect.contains(pos):
            return
        # When the cursor's over a dot, the dot itself is the focus —
        # don't double-decorate.
        if self._dot_at(pos) >= 0:
            return
        p.setPen(QPen(HOVER_LINE, 1, Qt.DashLine))
        p.drawLine(QPointF(pos.x(), rect.top()), QPointF(pos.x(), rect.bottom()))
        hz = _x_to_hz(pos.x(), rect.left(), rect.right())
        db = self._curve_db_at_x(pos.x(), rect)
        text = f"{_format_freq_label(hz)}  ·  {db:+.1f} dB"
        f = QFont(self.font())
        f.setPointSizeF(max(8.5, f.pointSizeF() - 0.5))
        p.setFont(f)
        fm = QFontMetrics(f)
        pad_x, pad_y = 9.0, 4.0
        tw = fm.horizontalAdvance(text)
        th = fm.height()
        ox = pos.x() + 12
        if ox + tw + 2 * pad_x > rect.right():
            ox = pos.x() - 12 - tw - 2 * pad_x
        oy = max(rect.top() + 4, pos.y() - th / 2 - pad_y)
        pill = QRectF(ox, oy, tw + 2 * pad_x, th + 2 * pad_y)
        p.setPen(Qt.NoPen)
        p.setBrush(TOOLTIP_BG)
        p.drawRoundedRect(pill, 6, 6)
        p.setPen(QColor(TOOLTIP_TEXT))
        p.drawText(pill, Qt.AlignCenter, text)

    def _paint_slot_full_banner(self, p: QPainter, rect: QRectF) -> None:
        """Brief banner shown after a placement click that found no
        free slot. Auto-hides via the wall-clock timestamp set in
        mousePressEvent."""
        now_ms = QDateTime.currentMSecsSinceEpoch()
        if now_ms >= self._slot_full_banner_until_ms:
            return
        text = self.tr("All 10 bands are in use — drag or remove an existing point first")
        f = QFont(self.font())
        f.setPointSizeF(max(9.5, f.pointSizeF()))
        f.setBold(True)
        p.setFont(f)
        fm = QFontMetrics(f)
        pad_x, pad_y = 14.0, 8.0
        tw = fm.horizontalAdvance(text)
        th = fm.height()
        bw = tw + 2 * pad_x
        bh = th + 2 * pad_y
        bx = rect.left() + (rect.width() - bw) / 2.0
        by = rect.top() + 8.0
        banner = QRectF(bx, by, bw, bh)
        # Warm amber so it reads as "advisory", not red-alert error.
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 152, 0, 235))
        p.drawRoundedRect(banner, 6, 6)
        p.setPen(QColor("#1A1B2E"))
        p.drawText(banner, Qt.AlignCenter, text)

    def _curve_db_at_x(self, x: float, rect: QRectF) -> float:
        """Linear-interpolate against the cached curve points. Used
        for the hover readout — running the full biquad sum just to
        show a tooltip would defeat the caching."""
        pts = self._curve_points
        if not pts or len(pts) < 2:
            return 0.0
        first = pts[0].x()
        last = pts[-1].x()
        if x <= first:
            return _y_to_db(pts[0].y(), rect.top(), rect.bottom())
        if x >= last:
            return _y_to_db(pts[-1].y(), rect.top(), rect.bottom())
        n = len(pts)
        frac = (x - first) / (last - first)
        idx_f = frac * (n - 1)
        i0 = int(idx_f)
        i1 = min(i0 + 1, n - 1)
        t = idx_f - i0
        y = pts[i0].y() * (1 - t) + pts[i1].y() * t
        return _y_to_db(y, rect.top(), rect.bottom())
