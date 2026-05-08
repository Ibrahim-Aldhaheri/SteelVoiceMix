"""Sonar-style alternative EQ view.

Renders the 10-band parametric EQ as a logarithmic-frequency / linear-dB
graph with draggable dots per band. Mirrors the look and interaction
model of SteelSeries Sonar's EQ tab. Lives next to the slider grid in
gui/tabs/equalizer.py and reads/writes the same _bands_by_channel dict
the sliders do, so the two views stay in sync mid-session.

Not yet implemented (planned in project memory under "Sonar-style EQ
view"): per-game zone-label dictionary, Bass/Voice/Treble global
sliders below the graph, right-click filter-type / Q editor popup,
reset-to-loaded-preset button. This file ships only the core widget so
beta39 stays reviewable.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
)
from PySide6.QtWidgets import QSizePolicy, QWidget


# Plot range — matches the slider grid (±12 dB) and the audible band.
FREQ_MIN_HZ = 20.0
FREQ_MAX_HZ = 20000.0
GAIN_MIN_DB = -12.0
GAIN_MAX_DB = 12.0

# The transfer-function shape near Nyquist depends on fs, so a wrong
# fs would draw a curve that doesn't match what the user hears. The
# daemon's filter chain runs at PipeWire's default 48 kHz.
SAMPLE_RATE_HZ = 48000.0

# Pixel samples along the X axis for the response curve. 256 is smooth
# at typical widget widths and cheap enough to recompute every drag tick.
RESPONSE_SAMPLES = 256

DOT_RADIUS_PX = 9
DOT_HIT_RADIUS_PX = 14

# A band is treated as "active" (renders a dot) iff its gain magnitude
# is at least this much. Hides the 10 default-flat dots so a fresh
# graph reads as Sonar's clean curve. The user reveals dots by
# clicking — see mousePressEvent.
VISIBILITY_EPS_DB = 0.05

# When the user clicks empty space exactly on the 0 dB line, snap the
# new dot to a small non-zero gain so it stays visible. Without this
# the placed dot would vanish the moment the user releases.
PLACEMENT_FLOOR_DB = 0.5

# Default zone labels (no game loaded). Per-game overrides land in
# gui/presets/eq_zones.json in a follow-up session.
DEFAULT_ZONES: list[tuple[str, float, float]] = [
    ("SUB BASS", 20.0, 60.0),
    ("BASS", 60.0, 250.0),
    ("LOW MIDS", 250.0, 500.0),
    ("MID RANGE", 500.0, 2000.0),
    ("UPPER MIDS", 2000.0, 6000.0),
    ("HIGHS", 6000.0, 20000.0),
]

# Per-band dot colors. Cycles through a perceptually distinct palette
# so the 10 dots stay readable when they cluster on a flat preset.
DOT_COLORS = [
    "#E53935", "#FB8C00", "#FDD835", "#7CB342", "#26A69A",
    "#1E88E5", "#5E35B1", "#D81B60", "#8D6E63", "#546E7A",
]


def _hz_to_x(hz: float, plot_left: float, plot_right: float) -> float:
    lhz = math.log10(max(hz, FREQ_MIN_HZ))
    lo = math.log10(FREQ_MIN_HZ)
    hi = math.log10(FREQ_MAX_HZ)
    return plot_left + (lhz - lo) / (hi - lo) * (plot_right - plot_left)


def _x_to_hz(x: float, plot_left: float, plot_right: float) -> float:
    if plot_right <= plot_left:
        return FREQ_MIN_HZ
    frac = (x - plot_left) / (plot_right - plot_left)
    frac = max(0.0, min(1.0, frac))
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
    frac = (plot_bottom - y) / (plot_bottom - plot_top)
    frac = max(0.0, min(1.0, frac))
    return GAIN_MIN_DB + frac * (GAIN_MAX_DB - GAIN_MIN_DB)


def _biquad_coeffs(
    freq_hz: float, q: float, gain_db: float, ftype: str,
) -> tuple[float, float, float, float, float, float]:
    """RBJ cookbook biquad coefficients for visualization. The real
    audio runs in PipeWire's filter chain — these match its math so the
    drawn curve and the audible response stay aligned."""
    A = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * math.pi * freq_hz / SAMPLE_RATE_HZ
    cw = math.cos(w0)
    sw = math.sin(w0)
    alpha = sw / (2.0 * max(q, 0.01))
    if ftype == "peaking":
        b0 = 1.0 + alpha * A
        b1 = -2.0 * cw
        b2 = 1.0 - alpha * A
        a0 = 1.0 + alpha / A
        a1 = -2.0 * cw
        a2 = 1.0 - alpha / A
    elif ftype == "lowshelf":
        sqA = math.sqrt(A)
        b0 = A * ((A + 1) - (A - 1) * cw + 2.0 * sqA * alpha)
        b1 = 2.0 * A * ((A - 1) - (A + 1) * cw)
        b2 = A * ((A + 1) - (A - 1) * cw - 2.0 * sqA * alpha)
        a0 = (A + 1) + (A - 1) * cw + 2.0 * sqA * alpha
        a1 = -2.0 * ((A - 1) + (A + 1) * cw)
        a2 = (A + 1) + (A - 1) * cw - 2.0 * sqA * alpha
    else:
        sqA = math.sqrt(A)
        b0 = A * ((A + 1) + (A - 1) * cw + 2.0 * sqA * alpha)
        b1 = -2.0 * A * ((A - 1) + (A + 1) * cw)
        b2 = A * ((A + 1) + (A - 1) * cw - 2.0 * sqA * alpha)
        a0 = (A + 1) - (A - 1) * cw + 2.0 * sqA * alpha
        a1 = 2.0 * ((A - 1) - (A + 1) * cw)
        a2 = (A + 1) - (A - 1) * cw - 2.0 * sqA * alpha
    return b0, b1, b2, a0, a1, a2


def _band_response_db(band: dict, freq_hz: float) -> float:
    if not band.get("enabled", True):
        return 0.0
    gain = float(band.get("gain", 0.0))
    if abs(gain) < 1e-4:
        return 0.0
    b0, b1, b2, a0, a1, a2 = _biquad_coeffs(
        float(band.get("freq", 1000.0)),
        float(band.get("q", 1.0)),
        gain,
        str(band.get("type", "peaking")),
    )
    w = 2.0 * math.pi * freq_hz / SAMPLE_RATE_HZ
    cw1, sw1 = math.cos(-w), math.sin(-w)
    cw2, sw2 = math.cos(-2.0 * w), math.sin(-2.0 * w)
    nr = b0 + b1 * cw1 + b2 * cw2
    ni = b1 * sw1 + b2 * sw2
    dr = a0 + a1 * cw1 + a2 * cw2
    di = a1 * sw1 + a2 * sw2
    num_mag = math.hypot(nr, ni)
    den_mag = math.hypot(dr, di)
    if den_mag <= 0:
        return 0.0
    return 20.0 * math.log10(max(num_mag / den_mag, 1e-9))


class EqGraphWidget(QWidget):
    """Sonar-style EQ view: dot-on-a-curve graph with zone-label header.

    Signals:
      bandChanged(int, float, float):
        Emitted continuously while the user drags a dot. Args are
        (band_index 0..N-1, freq_hz, gain_db). Owner uses this to
        update its bands dict and queue a debounced daemon commit.

      bandReleased(int):
        Emitted when the user releases a drag. Owner flushes any
        pending commits and persists the band state — mirrors
        _on_slider_released in the slider grid.
    """

    bandChanged = Signal(int, float, float)
    bandReleased = Signal(int)

    PLOT_PAD_LEFT = 36
    PLOT_PAD_RIGHT = 12
    PLOT_PAD_TOP = 22
    PLOT_PAD_BOTTOM = 24

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._bands: list[dict] = []
        self._zones: list[tuple[str, float, float]] = list(DEFAULT_ZONES)
        self._dragging_band: int | None = None
        self._drag_offset: tuple[float, float] = (0.0, 0.0)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumHeight(280)
        self.setMouseTracking(True)

    def set_bands(self, bands: list[dict]) -> None:
        """Replace the bands shown. Stores shallow copies so external
        mutations between paint calls can't corrupt the curve.

        Skipped while the user is actively dragging a dot — daemon
        echoes of the in-flight commit would otherwise yank the dot
        out from under the cursor. The release handler always sends a
        fresh commit, and the next echo after release lands cleanly."""
        if self._dragging_band is not None:
            return
        self._bands = [dict(b) for b in bands]
        self.update()

    def set_zones(self, zones: list[tuple[str, float, float]] | None) -> None:
        self._zones = list(zones) if zones else list(DEFAULT_ZONES)
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

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.LeftButton or not self._bands:
            return
        pos = event.position()
        rect = self._plot_rect()
        if not rect.contains(pos):
            return
        # First try to grab an existing visible dot.
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
        if best_idx < 0:
            # Empty space → place a new dot. Find the unused slot
            # whose default freq sits closest to the click X — that
            # way clicking near 100 Hz uses a low-band slot, near
            # 5 kHz uses a high-band slot, and we don't fight the
            # band-type semantics (lowshelf at slot 0 etc.).
            best_idx = self._pick_placement_slot(pos.x())
            if best_idx < 0:
                return  # all 10 slots in use; nothing to place
            new_freq, new_gain = self._click_to_band_coords(pos)
            band = self._bands[best_idx]
            ftype = str(band.get("type", "peaking"))
            # Shelves carry their corner freq — moving them would
            # rewrite the shelf. Just bump their gain.
            if ftype not in ("lowshelf", "highshelf"):
                band["freq"] = new_freq
            band["gain"] = new_gain
            self.bandChanged.emit(best_idx, float(band["freq"]), new_gain)
        # Either path: enter drag-tracking so the user can refine the
        # placement without releasing the mouse.
        self._dragging_band = best_idx
        dot = self._band_dot_pos(self._bands[best_idx])
        self._drag_offset = (pos.x() - dot.x(), pos.y() - dot.y())
        self.setCursor(Qt.ClosedHandCursor)
        self.update()
        event.accept()

    def _band_is_visible(self, band: dict) -> bool:
        """A band renders as a dot iff it actively shapes the curve.
        Disabled bands and bands with effectively zero gain are
        invisible — keeps the graph clean before the user has placed
        anything."""
        if not band.get("enabled", True):
            return False
        return abs(float(band.get("gain", 0.0))) >= VISIBILITY_EPS_DB

    def _click_to_band_coords(self, pos: QPointF) -> tuple[float, float]:
        """Click position → (freq_hz, gain_db). Snaps gain away from
        exactly 0 dB so the placed dot stays visible after release."""
        r = self._plot_rect()
        freq = _x_to_hz(pos.x(), r.left(), r.right())
        gain = _y_to_db(pos.y(), r.top(), r.bottom())
        if abs(gain) < PLACEMENT_FLOOR_DB:
            gain = PLACEMENT_FLOOR_DB if gain >= 0 else -PLACEMENT_FLOOR_DB
        return freq, gain

    def _pick_placement_slot(self, click_x: float) -> int:
        """Pick the band slot to use for a new placement. Prefer an
        unused (gain-near-zero) peaking slot whose default freq is
        closest to the click. Falls back to shelves if nothing else
        is free, and returns -1 only when every slot is in use."""
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
            # Distance in log space — feels right on a log axis.
            dist = abs(math.log10(max(band_hz, 1.0)) -
                       math.log10(max(click_hz, 1.0)))
            ftype = str(band.get("type", "peaking"))
            if ftype == "peaking":
                if dist < best_peak_dist:
                    best_peak_dist = dist
                    best_peak = idx
            else:
                if dist < best_shelf_dist:
                    best_shelf_dist = dist
                    best_shelf = idx
        return best_peak if best_peak >= 0 else best_shelf

    def contextMenuEvent(self, event) -> None:
        """Right-click on a dot → reset that band's gain to 0, hiding
        the dot. The slot stays in the array (we always carry 10
        bands) but stops shaping the curve."""
        if not self._bands:
            return
        pos = event.pos()
        for idx, band in enumerate(self._bands):
            if not self._band_is_visible(band):
                continue
            dot = self._band_dot_pos(band)
            if math.hypot(pos.x() - dot.x(), pos.y() - dot.y()) <= DOT_HIT_RADIUS_PX:
                band["gain"] = 0.0
                self.bandChanged.emit(idx, float(band.get("freq", 1000.0)), 0.0)
                self.bandReleased.emit(idx)
                self.update()
                event.accept()
                return

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._dragging_band is None:
            self._update_hover_cursor(event.position())
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
        # Shelves: lock freq for the corner shelves to keep their tilt
        # role. Sonar doesn't move shelf corners either.
        ftype = str(band.get("type", "peaking"))
        if ftype in ("lowshelf", "highshelf"):
            new_freq = float(band.get("freq", new_freq))
        else:
            new_freq = max(FREQ_MIN_HZ * 1.1, min(FREQ_MAX_HZ * 0.9, new_freq))
        band["freq"] = new_freq
        band["gain"] = new_gain
        self.bandChanged.emit(idx, new_freq, new_gain)
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.LeftButton or self._dragging_band is None:
            return
        idx = self._dragging_band
        self._dragging_band = None
        self.unsetCursor()
        self.bandReleased.emit(idx)

    def _update_hover_cursor(self, pos: QPointF) -> None:
        for band in self._bands:
            if not self._band_is_visible(band):
                continue
            dot = self._band_dot_pos(band)
            if math.hypot(pos.x() - dot.x(), pos.y() - dot.y()) <= DOT_HIT_RADIUS_PX:
                self.setCursor(Qt.OpenHandCursor)
                return
        # Crosshair over the plot signals "click here to place a new
        # dot"; default cursor outside the plot rect.
        if self._plot_rect().contains(pos):
            self.setCursor(Qt.CrossCursor)
        else:
            self.unsetCursor()

    def paintEvent(self, _event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        rect = self._plot_rect()
        self._paint_zones(p, rect)
        self._paint_grid(p, rect)
        self._paint_curve(p, rect)
        self._paint_dots(p, rect)
        if not any(self._band_is_visible(b) for b in self._bands):
            self._paint_empty_hint(p, rect)

    def _paint_empty_hint(self, p: QPainter, rect: QRectF) -> None:
        """Centered hint shown when no bands are active. Disappears
        as soon as the user places a dot."""
        font = QFont(self.font())
        font.setItalic(True)
        font.setPointSizeF(max(9.0, font.pointSizeF()))
        p.setFont(font)
        p.setPen(QColor(255, 255, 255, 130))
        p.drawText(rect, Qt.AlignCenter,
                   self.tr("Click anywhere on the graph to add a point"))

    def _paint_zones(self, p: QPainter, rect: QRectF) -> None:
        strip_h = self.PLOT_PAD_TOP - 4
        font = QFont(self.font())
        font.setPointSizeF(max(7.5, font.pointSizeF() - 1))
        font.setBold(True)
        p.setFont(font)
        fm = QFontMetrics(font)
        for i, (label, f_lo, f_hi) in enumerate(self._zones):
            x_lo = _hz_to_x(f_lo, rect.left(), rect.right())
            x_hi = _hz_to_x(f_hi, rect.left(), rect.right())
            zone_rect = QRectF(x_lo, 2.0, max(1.0, x_hi - x_lo), strip_h)
            shade = QColor("#2A2F38") if i % 2 == 0 else QColor("#363B45")
            p.fillRect(zone_rect, shade)
            p.setPen(QColor("#E0E0E0"))
            text = label
            if fm.horizontalAdvance(text) > zone_rect.width() - 4:
                text = fm.elidedText(text, Qt.ElideRight, int(zone_rect.width() - 4))
            p.drawText(zone_rect, Qt.AlignCenter, text)

    def _paint_grid(self, p: QPainter, rect: QRectF) -> None:
        gridline_minor = QPen(QColor(255, 255, 255, 26), 1)
        gridline_zero = QPen(QColor(255, 255, 255, 90), 1)
        font = QFont(self.font())
        font.setPointSizeF(max(7.0, font.pointSizeF() - 2))
        p.setFont(font)
        for db in (-12, -6, 0, 6, 12):
            y = _db_to_y(float(db), rect.top(), rect.bottom())
            p.setPen(gridline_zero if db == 0 else gridline_minor)
            p.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
            p.setPen(QColor("#9AA0A6"))
            label = f"{db:+d}" if db != 0 else "0"
            p.drawText(
                QRectF(0, y - 8, rect.left() - 4, 16),
                Qt.AlignRight | Qt.AlignVCenter, label,
            )
        for hz, label in ((100, "100"), (1000, "1k"), (10000, "10k")):
            x = _hz_to_x(hz, rect.left(), rect.right())
            p.setPen(gridline_minor)
            p.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
            p.setPen(QColor("#9AA0A6"))
            p.drawText(
                QRectF(x - 24, rect.bottom() + 2, 48, 16),
                Qt.AlignCenter, label,
            )
        p.setPen(QPen(QColor(255, 255, 255, 60), 1))
        p.drawRect(rect)

    def _paint_curve(self, p: QPainter, rect: QRectF) -> None:
        if not self._bands:
            return
        path = QPainterPath()
        zero_y = _db_to_y(0.0, rect.top(), rect.bottom())
        fill_path = QPainterPath()
        fill_path.moveTo(rect.left(), zero_y)
        first = True
        last_pt: tuple[float, float] | None = None
        for i in range(RESPONSE_SAMPLES):
            frac = i / (RESPONSE_SAMPLES - 1)
            x = rect.left() + frac * rect.width()
            hz = _x_to_hz(x, rect.left(), rect.right())
            db = sum(_band_response_db(b, hz) for b in self._bands)
            db = max(GAIN_MIN_DB, min(GAIN_MAX_DB, db))
            y = _db_to_y(db, rect.top(), rect.bottom())
            if first:
                path.moveTo(x, y)
                first = False
            else:
                path.lineTo(x, y)
            fill_path.lineTo(x, y)
            last_pt = (x, y)
        if last_pt is not None:
            fill_path.lineTo(last_pt[0], zero_y)
        fill_path.lineTo(rect.left(), zero_y)
        # Green above 0 dB, red below — Sonar-style at-a-glance read of
        # where the user is boosting vs cutting.
        grad = QLinearGradient(0, rect.top(), 0, rect.bottom())
        grad.setColorAt(0.0, QColor(76, 175, 80, 90))
        grad.setColorAt(0.5, QColor(76, 175, 80, 0))
        grad.setColorAt(0.5001, QColor(229, 57, 53, 0))
        grad.setColorAt(1.0, QColor(229, 57, 53, 90))
        p.fillPath(fill_path, grad)
        p.setPen(QPen(QColor("#FAFAFA"), 1.6))
        p.drawPath(path)

    def _paint_dots(self, p: QPainter, rect: QRectF) -> None:
        for idx, band in enumerate(self._bands):
            # Hide flat / disabled bands. The dragged band is exempt:
            # it might be momentarily passing through 0 dB while the
            # user is shaping it.
            if not self._band_is_visible(band) and self._dragging_band != idx:
                continue
            dot = self._band_dot_pos(band)
            color = QColor(DOT_COLORS[idx % len(DOT_COLORS)])
            if not band.get("enabled", True):
                color.setAlpha(110)
            highlight = (self._dragging_band == idx)
            p.setBrush(color)
            outline = QColor("#FAFAFA") if highlight else QColor(0, 0, 0, 180)
            p.setPen(QPen(outline, 2.0 if highlight else 1.2))
            p.drawEllipse(dot, DOT_RADIUS_PX, DOT_RADIUS_PX)
            p.setPen(QColor("#FAFAFA"))
            f = QFont(self.font())
            f.setPointSizeF(max(6.5, f.pointSizeF() - 2))
            f.setBold(True)
            p.setFont(f)
            p.drawText(
                QRectF(
                    dot.x() - DOT_RADIUS_PX, dot.y() - DOT_RADIUS_PX,
                    2 * DOT_RADIUS_PX, 2 * DOT_RADIUS_PX,
                ),
                Qt.AlignCenter,
                str(idx + 1),
            )
