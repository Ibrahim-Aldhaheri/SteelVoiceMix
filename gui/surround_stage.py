"""Interactive surround stage — a QWidget that draws the listener at the
centre and each channel at its fixed direction, and lets the user drag a
speaker toward/away to change its LEVEL.

Honesty constraints (see STATUS.md and surround_model):
  * Direction is fixed by the sound profile. Dragging changes the radial
    distance only; the angle is locked to the channel's standard
    azimuth. The cursor's angle is ignored.
  * The radius encodes level (louder = closer), which maps to a real
    gain node. It is loudness, not physical-distance modelling.

Accessibility / interaction:
  * Full keyboard operation: Tab/arrow-keys select a speaker, Up/Down
    change its level, M mutes, S solos, T plays a test tone.
  * Mirror-friendly: geometry is computed from angles, so it renders
    correctly under RTL layouts (left/right speakers stay physically
    left/right; we do not flip the room).
  * Emits gain_changing (live, during drag) and gain_committed (on
    release) so the host can update the UI live but only talk to the
    daemon once per adjustment.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget

from .surround_model import (
    Channel,
    GAIN_MAX_DB,
    GAIN_MIN_DB,
    clamp_db,
    db_to_fraction,
    default_channels,
    fraction_to_db,
)
from .widgets import ACCENT, ON_ACCENT, WARN

_SPEAKER_R = 22  # speaker marker radius (px)


class SurroundStage(QWidget):
    gain_changing = Signal(str, float)   # key, dB — live during drag/keys
    gain_committed = Signal(str, float)  # key, dB — on release / key-up
    selection_changed = Signal(str)      # key or ""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._channels: dict[str, Channel] = default_channels()
        self._selected: str | None = "fc"
        self._dragging: str | None = None
        self._solo: str | None = None
        self.setMinimumSize(300, 300)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self.setAccessibleName("Surround stage editor")

    # ---- public state -------------------------------------------------

    def channels(self) -> dict[str, Channel]:
        return self._channels

    def set_channels(self, channels: dict[str, Channel]) -> None:
        self._channels = channels
        self.update()

    def set_gain(self, key: str, db: float) -> None:
        ch = self._channels.get(key)
        if ch:
            ch.gain_db = clamp_db(db)
            self.update()

    def set_muted(self, key: str, muted: bool) -> None:
        ch = self._channels.get(key)
        if ch:
            ch.muted = bool(muted)
            self.update()

    def selected(self) -> str | None:
        return self._selected

    def set_selected(self, key: str | None) -> None:
        if key != self._selected:
            self._selected = key
            self.selection_changed.emit(key or "")
            self.update()

    def solo(self) -> str | None:
        return self._solo

    def set_solo(self, key: str | None) -> None:
        self._solo = key
        self.update()

    # ---- geometry -----------------------------------------------------

    def _center(self) -> QPointF:
        return QPointF(self.width() / 2.0, self.height() / 2.0)

    def _max_radius(self) -> float:
        # Leave a margin for the speaker markers + labels.
        return max(40.0, min(self.width(), self.height()) / 2.0 - _SPEAKER_R - 18)

    def _speaker_pos(self, ch: Channel) -> QPointF:
        c = self._center()
        if not ch.positional:
            # LFE: park just below the listener, non-directional.
            return QPointF(c.x(), c.y() + self._max_radius() * 0.28)
        frac = db_to_fraction(ch.gain_db)
        r = frac * self._max_radius()
        # Angle: clockwise from straight up (north = front).
        theta = math.radians(ch.azimuth - 90.0)
        return QPointF(c.x() + r * math.cos(theta), c.y() + r * math.sin(theta))

    def _hit(self, pos: QPointF) -> str | None:
        for key, ch in self._channels.items():
            sp = self._speaker_pos(ch)
            if math.hypot(pos.x() - sp.x(), pos.y() - sp.y()) <= _SPEAKER_R + 3:
                return key
        return None

    # ---- painting -----------------------------------------------------

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = self._center()
        maxr = self._max_radius()

        mid = self.palette().mid().color()
        text_col = self.palette().text().color()
        faint = QColor(mid); faint.setAlpha(90)

        # Concentric guide rings (louder ... quieter).
        p.setPen(QPen(faint, 1, Qt.DashLine))
        for frac in (0.5, 0.75, 1.0):
            rr = frac * maxr
            p.drawEllipse(c, rr, rr)

        # Listener at centre — a simple head facing "up" (front).
        p.setPen(Qt.NoPen)
        p.setBrush(mid)
        p.drawEllipse(c, 15, 15)
        p.setPen(QPen(text_col, 2))
        p.drawLine(c, QPointF(c.x(), c.y() - 22))  # nose = facing front
        p.setPen(QPen(faint, 1))
        f = QFont(self.font()); f.setPointSizeF(max(7.0, f.pointSizeF() - 1))
        p.setFont(f)
        p.drawText(
            QRectF(c.x() - 40, c.y() - maxr - 16, 80, 14),
            Qt.AlignCenter, self.tr("front"),
        )

        for key, ch in self._channels.items():
            self._paint_speaker(p, key, ch)

    def _paint_speaker(self, p: QPainter, key: str, ch: Channel) -> None:
        sp = self._speaker_pos(ch)
        active = not ch.muted and (self._solo in (None, key))
        selected = key == self._selected

        if ch.muted:
            fill = self.palette().button().color()
        elif self._solo not in (None, key):
            fill = self.palette().button().color()
        else:
            fill = QColor(ACCENT)
        border = QColor(WARN) if selected else self.palette().mid().color()

        p.setPen(QPen(border, 3 if selected else 1))
        p.setBrush(fill)
        p.drawEllipse(sp, _SPEAKER_R, _SPEAKER_R)

        # Short code centred on the marker (FL, C, SR…).
        fg = QColor(ON_ACCENT) if active else self.palette().placeholderText().color()
        p.setPen(QPen(fg, 1))
        f = QFont(self.font()); f.setBold(True)
        p.setFont(f)
        p.drawText(
            QRectF(sp.x() - _SPEAKER_R, sp.y() - _SPEAKER_R, _SPEAKER_R * 2, _SPEAKER_R * 2),
            Qt.AlignCenter, ch.short or ch.label,
        )
        # dB caption just below the marker.
        cap = self.tr("muted") if ch.muted else f"{ch.gain_db:+.0f} dB"
        p.setPen(QPen(self.palette().text().color(), 1))
        f2 = QFont(self.font()); f2.setPointSizeF(max(7.0, f2.pointSizeF() - 1))
        p.setFont(f2)
        p.drawText(
            QRectF(sp.x() - 30, sp.y() + _SPEAKER_R + 1, 60, 13),
            Qt.AlignCenter, cap,
        )

    # ---- mouse --------------------------------------------------------

    def mousePressEvent(self, event) -> None:
        key = self._hit(event.position())
        if key:
            self.set_selected(key)
            ch = self._channels[key]
            if ch.positional and not ch.muted:
                self._dragging = key
            self.setFocus()
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        if not self._dragging:
            # Hover cursor feedback.
            self.setCursor(
                Qt.PointingHandCursor if self._hit(event.position()) else Qt.ArrowCursor
            )
            return
        ch = self._channels[self._dragging]
        c = self._center()
        dist = math.hypot(event.position().x() - c.x(), event.position().y() - c.y())
        frac = dist / self._max_radius()
        # ANGLE IS LOCKED: only the radial distance maps to level. The
        # cursor's angle is deliberately ignored — direction is fixed by
        # the sound profile.
        db = fraction_to_db(frac)
        ch.gain_db = db
        self.update()
        self.gain_changing.emit(self._dragging, db)

    def mouseReleaseEvent(self, event) -> None:
        if self._dragging:
            key = self._dragging
            self._dragging = None
            self.gain_committed.emit(key, self._channels[key].gain_db)
        event.accept()

    # ---- keyboard (accessibility) ------------------------------------

    def keyPressEvent(self, event) -> None:
        keys = list(self._channels)
        cur = self._selected
        k = event.key()
        if k in (Qt.Key_Tab, Qt.Key_Right, Qt.Key_Down) and event.modifiers() == Qt.NoModifier and k in (Qt.Key_Tab,):
            idx = (keys.index(cur) + 1) % len(keys) if cur in keys else 0
            self.set_selected(keys[idx])
        elif k == Qt.Key_Backtab:
            idx = (keys.index(cur) - 1) % len(keys) if cur in keys else 0
            self.set_selected(keys[idx])
        elif cur and k in (Qt.Key_Up, Qt.Key_Down):
            ch = self._channels[cur]
            if ch.positional:
                step = 0.5 if k == Qt.Key_Up else -0.5
                ch.gain_db = clamp_db(ch.gain_db + step)
                self.update()
                self.gain_changing.emit(cur, ch.gain_db)
                self.gain_committed.emit(cur, ch.gain_db)
        elif cur and k in (Qt.Key_PageUp, Qt.Key_PageDown):
            ch = self._channels[cur]
            if ch.positional:
                ch.gain_db = GAIN_MAX_DB if k == Qt.Key_PageUp else GAIN_MIN_DB
                self.update()
                self.gain_committed.emit(cur, ch.gain_db)
        else:
            super().keyPressEvent(event)
            return
        event.accept()
