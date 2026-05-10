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

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
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
from PySide6.QtWidgets import QSizePolicy, QWidget


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
# halo paints the wider radial bloom underneath. The halo alpha is
# baked into the hex (last two chars).
DOT_PALETTE: list[tuple[str, str]] = [
    ("#FF5577", "#FF557755"),  # 1
    ("#FF8855", "#FF885555"),  # 2
    ("#FFCC44", "#FFCC4455"),  # 3
    ("#A6E22E", "#A6E22E55"),  # 4
    ("#3DDBCD", "#3DDBCD55"),  # 5
    ("#5EA8FF", "#5EA8FF55"),  # 6
    ("#8B6BFF", "#8B6BFF55"),  # 7
    ("#D966FF", "#D966FF55"),  # 8
    ("#FF66B3", "#FF66B355"),  # 9
    ("#9CC0CF", "#9CC0CF55"),  # 10
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


class EqGraphWidget(QWidget):
    """Sonar-style EQ view: dot-on-a-curve graph with zone-label header.

    Signals:
      bandChanged(int, float, float):
        Drag tick. Args: (band_index 0..N-1, freq_hz, gain_db).
      bandReleased(int):
        Drag finished — owner flushes pending commits.
    """

    bandChanged = Signal(int, float, float)
    bandReleased = Signal(int)

    PLOT_PAD_LEFT = 42
    PLOT_PAD_RIGHT = 16
    PLOT_PAD_TOP = 32
    PLOT_PAD_BOTTOM = 30

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._bands: list[dict] = []
        self._zones: list[tuple[str, float, float]] = list(DEFAULT_ZONES)
        self._dragging_band: int | None = None
        self._drag_offset: tuple[float, float] = (0.0, 0.0)
        self._hover_pos: QPointF | None = None
        self._bg_pixmap: QPixmap | None = None
        self._curve_points: list[QPointF] | None = None
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumHeight(320)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_OpaquePaintEvent)

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
                return
            new_freq, new_gain = self._click_to_band_coords(pos)
            band = self._bands[best_idx]
            ftype = str(band.get("type", "peaking"))
            if ftype not in ("lowshelf", "highshelf"):
                band["freq"] = new_freq
            band["gain"] = new_gain
            self._curve_points = None
            self.bandChanged.emit(best_idx, float(band["freq"]), new_gain)
        self._dragging_band = best_idx
        dot = self._band_dot_pos(self._bands[best_idx])
        self._drag_offset = (pos.x() - dot.x(), pos.y() - dot.y())
        self.setCursor(Qt.ClosedHandCursor)
        self.update()
        event.accept()

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
            highlight = (self._dragging_band == idx)
            r = DOT_RADIUS_PX + (1.5 if highlight else 0.0)
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
