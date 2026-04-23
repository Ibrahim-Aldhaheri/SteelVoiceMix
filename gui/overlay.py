"""Floating ChatMix overlay that flashes on dial changes."""

from __future__ import annotations

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import QApplication, QWidget

from .settings import OVERLAY_ORIENTATIONS


class DialOverlay(QWidget):
    """Floating overlay that appears briefly when the dial is turned."""

    def __init__(self, parent=None):
        super().__init__(
            parent,
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool,
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)

        self.orientation = "horizontal"
        self.setFixedSize(340, 80)

        self.game_vol = 100
        self.chat_vol = 100
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)

    def set_orientation(self, orientation: str) -> None:
        if orientation not in OVERLAY_ORIENTATIONS:
            orientation = "horizontal"
        self.orientation = orientation
        if orientation == "vertical":
            self.setFixedSize(140, 170)
        else:
            self.setFixedSize(340, 80)
        self.update()

    def show_volumes(
        self,
        game_vol: int,
        chat_vol: int,
        position: str = "top-right",
    ) -> None:
        self.game_vol = game_vol
        self.chat_vol = chat_vol

        # Always show on the primary monitor. Previously we followed the
        # mouse cursor which meant whichever screen was "active" won —
        # confusing on multi-display setups where the headset and the
        # mouse weren't on the same screen. move() uses global virtual-
        # desktop coordinates, so we add the screen's absolute offset.
        screen_obj = QApplication.primaryScreen()
        g = screen_obj.geometry()

        margin = 24
        left = g.x()
        top = g.y()
        right = g.x() + g.width() - self.width()
        bottom = g.y() + g.height() - self.height()

        if position == "top-left":
            x, y = left + margin, top + margin
        elif position == "bottom-right":
            x, y = right - margin, bottom - margin
        elif position == "bottom-left":
            x, y = left + margin, bottom - margin
        elif position == "center":
            x = g.x() + (g.width() - self.width()) // 2
            y = g.y() + (g.height() - self.height()) // 2
        else:  # top-right (default)
            x, y = right - margin, top + margin
        self.move(x, y)

        self.update()
        self.show()
        self._hide_timer.start(1500)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        palette = QApplication.palette()
        bg_color = palette.window().color()
        bg_color.setAlpha(220)
        text_color = palette.windowText().color()

        painter.setBrush(bg_color)
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(self.rect(), 12, 12)

        if self.orientation == "vertical":
            self._paint_vertical(painter, text_color)
        else:
            self._paint_horizontal(painter, text_color)

        painter.end()

    def _paint_horizontal(self, painter: QPainter, text_color) -> None:
        painter.setPen(text_color)
        painter.setFont(QFont("", 11))

        bar_x, bar_w = 95, 200
        label_w = 85  # room for "🎮 Game" without truncation
        val_w = 34

        # Game row (top half)
        painter.drawText(
            8, 10, label_w, 22, Qt.AlignLeft | Qt.AlignVCenter, "🎮 Game"
        )
        painter.setBrush(QColor(60, 60, 60, 100))
        painter.drawRoundedRect(bar_x, 14, bar_w, 16, 4, 4)
        game_w = int(bar_w * self.game_vol / 100)
        painter.setBrush(QColor(76, 175, 80))
        painter.drawRoundedRect(bar_x, 14, game_w, 16, 4, 4)
        painter.setPen(text_color)
        painter.drawText(
            bar_x + bar_w + 4, 10, val_w, 22,
            Qt.AlignLeft | Qt.AlignVCenter, f"{self.game_vol}%",
        )

        # Chat row (bottom half)
        painter.drawText(
            8, 44, label_w, 22, Qt.AlignLeft | Qt.AlignVCenter, "💬 Chat"
        )
        painter.setBrush(QColor(60, 60, 60, 100))
        painter.drawRoundedRect(bar_x, 48, bar_w, 16, 4, 4)
        chat_w = int(bar_w * self.chat_vol / 100)
        painter.setBrush(QColor(33, 150, 243))
        painter.drawRoundedRect(bar_x, 48, chat_w, 16, 4, 4)
        painter.setPen(text_color)
        painter.drawText(
            bar_x + bar_w + 4, 44, val_w, 22,
            Qt.AlignLeft | Qt.AlignVCenter, f"{self.chat_vol}%",
        )

    def _paint_vertical(self, painter: QPainter, text_color) -> None:
        painter.setPen(text_color)
        painter.setFont(QFont("", 10))

        col_w = self.width() // 2
        bar_w = 20
        bar_h = 95
        bar_top = 30
        game_bar_x = col_w // 2 - bar_w // 2
        chat_bar_x = col_w + col_w // 2 - bar_w // 2

        painter.drawText(0, 4, col_w, 22, Qt.AlignCenter, "🎮 Game")
        painter.drawText(col_w, 4, col_w, 22, Qt.AlignCenter, "💬 Chat")

        # Game bar (bottom-up fill)
        painter.setBrush(QColor(60, 60, 60, 100))
        painter.drawRoundedRect(game_bar_x, bar_top, bar_w, bar_h, 4, 4)
        game_fill = int(bar_h * self.game_vol / 100)
        painter.setBrush(QColor(76, 175, 80))
        painter.drawRoundedRect(
            game_bar_x, bar_top + bar_h - game_fill, bar_w, game_fill, 4, 4
        )

        # Chat bar
        painter.setBrush(QColor(60, 60, 60, 100))
        painter.drawRoundedRect(chat_bar_x, bar_top, bar_w, bar_h, 4, 4)
        chat_fill = int(bar_h * self.chat_vol / 100)
        painter.setBrush(QColor(33, 150, 243))
        painter.drawRoundedRect(
            chat_bar_x, bar_top + bar_h - chat_fill, bar_w, chat_fill, 4, 4
        )

        painter.setPen(text_color)
        painter.drawText(
            0, bar_top + bar_h + 4, col_w, 20, Qt.AlignCenter, f"{self.game_vol}%"
        )
        painter.drawText(
            col_w, bar_top + bar_h + 4, col_w, 20, Qt.AlignCenter, f"{self.chat_vol}%"
        )
