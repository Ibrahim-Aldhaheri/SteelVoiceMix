"""Interactive surround stage — a QWidget that draws the listener at the
centre and each channel at its fixed direction, and lets the user drag a
speaker toward/away to change its LEVEL.

Honesty constraints (see STATUS.md and surround_model):
  * Direction is fixed by the sound profile. Dragging changes the radial
    distance only; the angle is locked to the channel's standard
    azimuth. The cursor's angle is ignored.
  * The radius encodes level (louder = closer), which maps to a real
    gain node. It is loudness, not physical-distance modelling.

Interaction / accessibility:
  * Full keyboard operation: Left/Right move the selection clockwise
    around the ring (mirrored under RTL), Up/Down change the selected
    speaker's level, PageUp/PageDown jump to the bounds, M mutes, S
    solos, T plays a test tone. Real Tab is left to normal focus
    traversal.
  * The stage reports its selection and the selected level through
    accessibleName, and draws a focus ring when it has keyboard focus.
  * Level changes stream out live via gain_changing during a drag or a
    run of key presses, and commit once (gain_committed) shortly after
    the last change — one daemon respawn per adjustment, like the EQ.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
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
from .widgets import ACCENT, ON_ACCENT

_SPEAKER_R = 20  # speaker marker radius (px)
# Delay after the last keyboard step before committing to the daemon,
# so holding an arrow key is one respawn, not a storm.
_COMMIT_DELAY_MS = 300


class SurroundStage(QWidget):
    gain_changing = Signal(str, float)   # key, dB — live during drag/keys
    gain_committed = Signal(str, float)  # key, dB — after the last change
    drag_started = Signal(str)           # key — so the host can snapshot undo
    selection_changed = Signal(str)      # key or ""
    mute_requested = Signal(str)         # key — keyboard M
    solo_requested = Signal(str)         # key — keyboard S
    test_requested = Signal(str)         # key — keyboard T

    def __init__(self, parent=None):
        super().__init__(parent)
        self._channels: dict[str, Channel] = default_channels()
        self._selected: str | None = "fc"
        self._dragging: str | None = None
        self._solo: str | None = None
        # Which speaker is briefly pulsing from a test tone (visual echo).
        self._testing: str | None = None
        self.setMinimumSize(300, 300)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)

        # Debounced keyboard-commit timer.
        self._commit_timer = QTimer(self)
        self._commit_timer.setSingleShot(True)
        self._commit_timer.setInterval(_COMMIT_DELAY_MS)
        self._commit_timer.timeout.connect(self._commit_pending)
        self._pending_commit: str | None = None

        self._test_timer = QTimer(self)
        self._test_timer.setSingleShot(True)
        self._test_timer.setInterval(1400)
        self._test_timer.timeout.connect(self._clear_testing)

        self._update_accessible()

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
            if key == self._selected:
                self._update_accessible()

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
            self._update_accessible()
            self.update()

    def solo(self) -> str | None:
        return self._solo

    def set_solo(self, key: str | None) -> None:
        self._solo = key
        self.update()

    def flash_test(self, key: str) -> None:
        """Briefly pulse a speaker to echo a test tone."""
        self._testing = key
        self._test_timer.start()
        self.update()

    # ---- clockwise selection order -----------------------------------

    def _ring_order(self) -> list[str]:
        """Channel keys ordered clockwise from the front (C), with the
        non-positional LFE last. Selection steps follow this ring rather
        than dict order so arrow keys move predictably around the room."""
        positional = [
            (ch.azimuth % 360.0, key)
            for key, ch in self._channels.items()
            if ch.positional
        ]
        positional.sort()
        order = [key for _, key in positional]
        order += [k for k, c in self._channels.items() if not c.positional]
        return order

    def _step_selection(self, forward: bool) -> None:
        order = self._ring_order()
        if not order:
            return
        # Mirror horizontal direction under RTL so "right" always feels
        # like moving toward the right of the screen.
        if self.layoutDirection() == Qt.RightToLeft:
            forward = not forward
        cur = self._selected if self._selected in order else order[0]
        idx = (order.index(cur) + (1 if forward else -1)) % len(order)
        self.set_selected(order[idx])

    # ---- geometry -----------------------------------------------------

    def _center(self) -> QPointF:
        return QPointF(self.width() / 2.0, self.height() / 2.0)

    def _max_radius(self) -> float:
        return max(40.0, min(self.width(), self.height()) / 2.0 - _SPEAKER_R - 20)

    def _speaker_pos(self, ch: Channel) -> QPointF:
        c = self._center()
        if not ch.positional:
            # LFE: park below the listener, out far enough not to merge
            # with the listener head.
            return QPointF(c.x(), c.y() + self._max_radius() * 0.42)
        frac = db_to_fraction(ch.gain_db)
        r = frac * self._max_radius()
        theta = math.radians(ch.azimuth - 90.0)
        return QPointF(c.x() + r * math.cos(theta), c.y() + r * math.sin(theta))

    def _hit(self, pos: QPointF) -> str | None:
        # Nearest marker within its radius — not first-in-dict, so a
        # click in an overlap region selects the speaker drawn on top
        # (closest centre), never a hidden one underneath.
        best_key = None
        best_dist = _SPEAKER_R + 4
        for key, ch in self._channels.items():
            sp = self._speaker_pos(ch)
            d = math.hypot(pos.x() - sp.x(), pos.y() - sp.y())
            if d <= best_dist:
                best_dist = d
                best_key = key
        return best_key

    # ---- painting -----------------------------------------------------

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = self._center()
        maxr = self._max_radius()

        mid = self.palette().mid().color()
        text_col = self.palette().text().color()
        faint = QColor(mid); faint.setAlpha(90)

        # Focus ring around the whole stage when it has keyboard focus,
        # so a keyboard user knows arrows will act here.
        if self.hasFocus():
            fp = QPen(QColor(ACCENT), 1, Qt.DashLine)
            p.setPen(fp)
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(self.rect().adjusted(2, 2, -3, -3), 8, 8)

        # Guide rings with a labelled 0 dB reference so "where is normal"
        # and "which way is louder" are visible without reading help.
        p.setPen(QPen(faint, 1, Qt.DashLine))
        p.setBrush(Qt.NoBrush)
        for frac in (0.5, 0.75, 1.0):
            rr = frac * maxr
            p.drawEllipse(c, rr, rr)
        zero_r = db_to_fraction(0.0) * maxr
        p.setPen(QPen(faint, 1))
        p.drawEllipse(c, zero_r, zero_r)
        f0 = QFont(self.font()); f0.setPointSizeF(max(7.0, f0.pointSizeF() - 2))
        p.setFont(f0)
        p.setPen(QPen(self.palette().placeholderText().color(), 1))
        p.drawText(
            QRectF(c.x() - 30, c.y() - zero_r - 13, 60, 12),
            Qt.AlignCenter, self.tr("0 dB"),
        )

        # Listener at centre facing "up" (front).
        p.setPen(Qt.NoPen)
        p.setBrush(mid)
        p.drawEllipse(c, 14, 14)
        p.setPen(QPen(text_col, 2))
        p.drawLine(c, QPointF(c.x(), c.y() - 20))
        p.setPen(QPen(faint, 1))
        p.setFont(f0)
        p.drawText(
            QRectF(c.x() - 40, c.y() - maxr - 15, 80, 13),
            Qt.AlignCenter, self.tr("front"),
        )

        for key, ch in self._channels.items():
            self._paint_speaker(p, key, ch)

    def _paint_speaker(self, p: QPainter, key: str, ch: Channel) -> None:
        sp = self._speaker_pos(ch)
        soloed_away = self._solo not in (None, key)
        active = not ch.muted and not soloed_away
        selected = key == self._selected
        is_solo = self._solo == key
        testing = key == self._testing

        if ch.muted:
            # Muted: hollow outline, clearly different from active.
            p.setBrush(self.palette().window().color())
            edge = self.palette().mid().color()
        elif soloed_away:
            # Soloed away: filled grey (dimmed), distinct from muted.
            p.setBrush(self.palette().button().color())
            edge = self.palette().mid().color()
        else:
            p.setBrush(QColor(ACCENT))
            edge = QColor(ACCENT)

        # Selection = accent ring (not the orange warning colour); solo
        # gets an extra outer halo; test pulses a wider ring.
        if selected:
            edge = QColor(ACCENT).darker(140)
        r = _SPEAKER_R + (2 if testing else 0)
        p.setPen(QPen(edge, 3 if selected else 1))
        p.drawEllipse(sp, r, r)
        if is_solo:
            p.setPen(QPen(QColor(ACCENT), 2))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(sp, r + 4, r + 4)
        if selected:
            p.setPen(QPen(QColor(ACCENT), 2, Qt.DashLine))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(sp, r + 6, r + 6)

        fg = QColor(ON_ACCENT) if active else self.palette().placeholderText().color()
        p.setPen(QPen(fg, 1))
        f = QFont(self.font()); f.setBold(True)
        p.setFont(f)
        p.setBrush(Qt.NoBrush)
        p.drawText(
            QRectF(sp.x() - _SPEAKER_R, sp.y() - _SPEAKER_R, _SPEAKER_R * 2, _SPEAKER_R * 2),
            Qt.AlignCenter, ch.short or ch.label,
        )
        cap = self.tr("muted") if ch.muted else self.tr("solo") if is_solo else f"{ch.gain_db:+.0f} dB"
        p.setPen(QPen(self.palette().text().color(), 1))
        f2 = QFont(self.font()); f2.setPointSizeF(max(7.0, f2.pointSizeF() - 1))
        p.setFont(f2)
        p.drawText(
            QRectF(sp.x() - 32, sp.y() + _SPEAKER_R + 1, 64, 13),
            Qt.AlignCenter, cap,
        )

    # ---- mouse --------------------------------------------------------

    def mousePressEvent(self, event) -> None:
        key = self._hit(event.position())
        if key:
            self.set_selected(key)
            ch = self._channels[key]
            if ch.positional:
                self._dragging = key
                self.drag_started.emit(key)  # host snapshots undo now
                self.setCursor(Qt.ClosedHandCursor)
            self.setFocus()
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        if not self._dragging:
            self.setCursor(
                Qt.PointingHandCursor if self._hit(event.position()) else Qt.ArrowCursor
            )
            return
        ch = self._channels[self._dragging]
        c = self._center()
        dist = math.hypot(event.position().x() - c.x(), event.position().y() - c.y())
        frac = dist / self._max_radius()
        # ANGLE IS LOCKED: only radial distance maps to level. Direction
        # is fixed by the sound profile.
        db = fraction_to_db(frac)
        ch.gain_db = db
        self.update()
        self._update_accessible()
        self.gain_changing.emit(self._dragging, db)

    def mouseReleaseEvent(self, event) -> None:
        if self._dragging:
            key = self._dragging
            self._dragging = None
            self.setCursor(Qt.ArrowCursor)
            self.gain_committed.emit(key, self._channels[key].gain_db)
        event.accept()

    # ---- keyboard -----------------------------------------------------

    def keyPressEvent(self, event) -> None:
        cur = self._selected
        k = event.key()
        mods = event.modifiers()
        if k in (Qt.Key_Right, Qt.Key_Left) and mods == Qt.NoModifier:
            self._step_selection(forward=(k == Qt.Key_Right))
        elif cur and k in (Qt.Key_Up, Qt.Key_Down) and self._channels[cur].positional:
            step = 0.5 if k == Qt.Key_Up else -0.5
            self._nudge(cur, self._channels[cur].gain_db + step)
        elif cur and k in (Qt.Key_PageUp, Qt.Key_PageDown) and self._channels[cur].positional:
            self._nudge(cur, GAIN_MAX_DB if k == Qt.Key_PageUp else GAIN_MIN_DB)
        elif cur and k == Qt.Key_M:
            self.mute_requested.emit(cur)
        elif cur and k == Qt.Key_S:
            self.solo_requested.emit(cur)
        elif cur and k == Qt.Key_T:
            self.test_requested.emit(cur)
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    def _nudge(self, key: str, db: float) -> None:
        ch = self._channels[key]
        # Snapshot undo once at the start of a key-repeat run.
        if self._pending_commit != key:
            self.drag_started.emit(key)
        ch.gain_db = clamp_db(db)
        self.update()
        self._update_accessible()
        self.gain_changing.emit(key, ch.gain_db)
        self._pending_commit = key
        self._commit_timer.start()

    def _commit_pending(self) -> None:
        key = self._pending_commit
        self._pending_commit = None
        if key and key in self._channels:
            self.gain_committed.emit(key, self._channels[key].gain_db)

    def _clear_testing(self) -> None:
        self._testing = None
        self.update()

    # ---- accessibility ------------------------------------------------

    def _update_accessible(self) -> None:
        key = self._selected
        ch = self._channels.get(key) if key else None
        if ch is None:
            self.setAccessibleName(self.tr("Surround stage — no speaker selected"))
            return
        if ch.muted:
            state = self.tr("muted")
        elif not ch.positional:
            state = self.tr("bass level {db:+.0f} decibels").format(db=ch.gain_db)
        else:
            state = self.tr("{db:+.0f} decibels").format(db=ch.gain_db)
        self.setAccessibleName(
            self.tr("Surround stage — {name} selected, {state}").format(
                name=ch.label, state=state,
            )
        )
