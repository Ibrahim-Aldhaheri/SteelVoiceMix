"""SearchableSelect — JS-style searchable dropdown widget.

Why this exists: PySide6's QComboBox-with-completer leaves the user
fighting the widget — the line edit holds the currently-selected
preset's name, so to search you first have to clear it; mouse-wheel
on the closed combo silently steps through every preset (very bad
when each step auto-applies); the completer popup is separate from
the main dropdown so the discovery experience is fragmented.

This widget mimics what react-select / vue-select / Headless UI
combo-boxes do on the web:

  • Closed state shows the current selection as a button.
  • Click opens a popup with a search field at the top + a filtered
    list below.
  • Typing filters the list immediately (substring, case-insensitive).
  • Up / Down keys navigate; Enter picks; Esc closes.
  • Mouse wheel does nothing — wheel events are eaten so accidental
    scrolling can't change selection.

API mirrors the bits of QComboBox we use in the EQ tab so the swap
is straightforward:
  - `addItem(text, userData=None)`, `clear()`, `count()`, `itemData(i)`,
    `itemText(i)`, `currentIndex()`, `currentData()`, `currentText()`
  - `setCurrentIndex(i)` (programmatic; doesn't fire `activated`)
  - Signals: `activated(int)` fires only on explicit user pick;
    `currentIndexChanged(int)` fires on any selection change.
  - `insertSeparator(idx)` so favourites can stay visually grouped.

Visual styling stays consistent with the rest of the GUI (button +
popup pick up palette colours; QSS hooks via QObject names).
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QEvent, QPoint, QSize, Qt, Signal
from PySide6.QtGui import QPainter, QPalette, QPolygon
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


class _Popup(QFrame):
    """The popup window used by SearchableSelect. A QFrame with the
    Qt.Popup window flag so it auto-closes on outside clicks and
    behaves like a proper menu."""

    def __init__(self, parent: "SearchableSelect"):
        super().__init__(parent, Qt.Popup)
        self.setObjectName("searchable-popup")
        self.setFrameShape(QFrame.NoFrame)

        self._owner = parent
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._on_search_text_changed)
        layout.addWidget(self.search)

        self.list_widget = QListWidget()
        self.list_widget.setUniformItemSizes(True)
        self.list_widget.itemClicked.connect(self._on_item_clicked)
        layout.addWidget(self.list_widget, 1)

        # Keyboard nav: Up / Down / Enter / Esc captured at the
        # search-field level so the user never has to manually click
        # into the list to navigate.
        self.search.installEventFilter(self)

    # ----------------------------------------------------- public API

    def populate(self) -> None:
        """Rebuild the visible list from the owner's items + the
        current search query."""
        query = self.search.text().strip().lower()
        self.list_widget.clear()
        for entry in self._owner._items:
            if entry.is_separator:
                if query:
                    # Hide separators while filtering — they only make
                    # sense as visual grouping for the unfiltered list.
                    continue
                sep = QListWidgetItem()
                sep.setFlags(Qt.NoItemFlags)
                sep.setSizeHint(QSize(0, 1))
                sep.setBackground(self.palette().color(QPalette.Mid))
                self.list_widget.addItem(sep)
                continue
            if query and query not in entry.text.lower():
                continue
            item = QListWidgetItem(entry.text)
            # Stash the items-array index in user role so the click
            # handler can resolve back to the canonical index without
            # rescanning by text.
            item.setData(Qt.UserRole, entry.index)
            self.list_widget.addItem(item)
        # If the current selection is visible in the filtered view,
        # highlight it. Otherwise highlight the first row so Enter
        # picks something sensible.
        sel_idx = self._owner._current_index
        for row in range(self.list_widget.count()):
            it = self.list_widget.item(row)
            if it.data(Qt.UserRole) == sel_idx:
                self.list_widget.setCurrentRow(row)
                break
        else:
            if self.list_widget.count() > 0:
                # Skip leading separators when picking the default.
                for row in range(self.list_widget.count()):
                    if self.list_widget.item(row).flags() != Qt.NoItemFlags:
                        self.list_widget.setCurrentRow(row)
                        break

    # ------------------------------------------------------- handlers

    def _on_search_text_changed(self, _text: str) -> None:
        self.populate()

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        if item.flags() == Qt.NoItemFlags:
            return  # Separator
        idx = int(item.data(Qt.UserRole))
        self._owner._commit_user_pick(idx)
        self.close()

    def eventFilter(self, obj, event) -> bool:
        if obj is self.search and event.type() == QEvent.KeyPress:
            key = event.key()
            if key == Qt.Key_Down:
                self._step_selection(1)
                return True
            if key == Qt.Key_Up:
                self._step_selection(-1)
                return True
            if key in (Qt.Key_Return, Qt.Key_Enter):
                cur = self.list_widget.currentItem()
                if cur is not None and cur.flags() != Qt.NoItemFlags:
                    self._on_item_clicked(cur)
                return True
            if key == Qt.Key_Escape:
                self.close()
                return True
        return super().eventFilter(obj, event)

    def _step_selection(self, direction: int) -> None:
        """Move the highlighted row by `direction` (skip separators)."""
        rows = self.list_widget.count()
        if rows == 0:
            return
        cur = self.list_widget.currentRow()
        if cur < 0:
            cur = 0 if direction > 0 else rows - 1
        new_row = cur
        for _ in range(rows):
            new_row = (new_row + direction) % rows
            it = self.list_widget.item(new_row)
            if it.flags() != Qt.NoItemFlags:
                self.list_widget.setCurrentRow(new_row)
                return

    # The popup auto-closes on outside-click thanks to Qt.Popup. We
    # also clear the search box so the next open starts fresh.
    def closeEvent(self, event) -> None:
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        super().closeEvent(event)


class _Item:
    __slots__ = ("text", "data", "index", "is_separator")

    def __init__(self, text: str, data: Any, index: int, is_separator: bool = False):
        self.text = text
        self.data = data
        self.index = index
        self.is_separator = is_separator


class _SelectButton(QPushButton):
    """Rebrand of QPushButton that ignores wheel events — needed
    because the EQ tab auto-applies on selection and a stray scroll
    would step through every preset."""

    def wheelEvent(self, event) -> None:
        event.ignore()


class SearchableSelect(QWidget):
    """Dropdown widget with an integrated search field. Drop-in
    replacement for QComboBox in the contexts where we use it."""

    activated = Signal(int)
    currentIndexChanged = Signal(int)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._items: list[_Item] = []
        self._current_index: int = -1

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._button = _SelectButton(self)
        self._button.setObjectName("searchable-button")
        self._button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._button.setStyleSheet(
            "text-align: left; padding-right: 24px;"
        )
        self._button.clicked.connect(self._open_popup)
        layout.addWidget(self._button)

        self._popup = _Popup(self)
        self._popup.hide()

    # ------------------------------------------------------- API mirror

    def addItem(self, text: str, userData: Any = None) -> None:
        idx = len(self._items)
        self._items.append(_Item(text, userData, idx))
        if self._current_index < 0:
            self._set_index(0, emit=False)

    def insertSeparator(self, _index: int) -> None:
        # The single-arg `_index` matches QComboBox's signature for
        # easy swap, but separators are always appended where they
        # naturally fall in the items list (we don't reorder).
        self._items.append(
            _Item("", None, len(self._items), is_separator=True)
        )

    def clear(self) -> None:
        self._items.clear()
        self._current_index = -1
        self._refresh_button_label()

    def count(self) -> int:
        return len(self._items)

    def itemText(self, i: int) -> str:
        if 0 <= i < len(self._items):
            return self._items[i].text
        return ""

    def itemData(self, i: int) -> Any:
        if 0 <= i < len(self._items):
            return self._items[i].data
        return None

    def currentIndex(self) -> int:
        return self._current_index

    def currentText(self) -> str:
        if 0 <= self._current_index < len(self._items):
            return self._items[self._current_index].text
        return ""

    def currentData(self) -> Any:
        if 0 <= self._current_index < len(self._items):
            return self._items[self._current_index].data
        return None

    def setCurrentIndex(self, i: int) -> None:
        """Programmatic selection — doesn't fire `activated` so
        internal repopulates don't trigger the EQ tab's auto-apply."""
        self._set_index(i, emit=False)

    # ----------------------------------------------------- internals

    def _open_popup(self) -> None:
        # Position the popup right under the button, matching its width.
        bottom_left = self.mapToGlobal(self._button.rect().bottomLeft())
        self._popup.setMinimumWidth(max(self._button.width(), 320))
        self._popup.populate()
        self._popup.move(bottom_left)
        self._popup.show()
        self._popup.search.setFocus()

    def _commit_user_pick(self, index: int) -> None:
        """Called by the popup when the user picks an item via click /
        Enter. Updates state + fires both signals."""
        if index < 0 or index >= len(self._items):
            return
        if self._items[index].is_separator:
            return
        changed = index != self._current_index
        self._set_index(index, emit=False)
        if changed:
            self.currentIndexChanged.emit(index)
        # `activated` always fires on a user-driven pick, even if the
        # selection didn't change — same semantics as QComboBox.
        self.activated.emit(index)

    def _set_index(self, i: int, *, emit: bool) -> None:
        if i == self._current_index:
            return
        if 0 <= i < len(self._items) and self._items[i].is_separator:
            return
        self._current_index = i if 0 <= i < len(self._items) else -1
        self._refresh_button_label()
        if emit:
            self.currentIndexChanged.emit(self._current_index)

    def _refresh_button_label(self) -> None:
        if self._current_index < 0 or self._current_index >= len(self._items):
            from PySide6.QtCore import QCoreApplication
            self._button.setText(QCoreApplication.translate("SearchableSelect", "Select…"))
            return
        # Right-arrow chevron drawn manually in paintEvent so we don't
        # depend on a theme-icon being installed. Button text is just
        # the current selection.
        self._button.setText(self._items[self._current_index].text)

    # Eat wheel events at the widget level too — the button does it
    # for hovers over the chevron, but a wheel event on the QWidget
    # itself would otherwise propagate.
    def wheelEvent(self, event) -> None:
        event.ignore()

    # ----------------------------------------------- chevron painting
    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        # Small caret on the right edge of the button so the user sees
        # this is a dropdown. Drawn ourselves because vanilla
        # QPushButton doesn't have a built-in arrow on the right.
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        color = self.palette().color(QPalette.Text)
        color.setAlpha(160)
        painter.setBrush(color)
        painter.setPen(Qt.NoPen)
        h = self.height()
        right = self.width() - 8
        size = 6
        cy = h // 2
        triangle = QPolygon(
            [
                QPoint(right - size, cy - size // 2),
                QPoint(right, cy - size // 2),
                QPoint(right - size // 2, cy + size // 2 + 1),
            ]
        )
        painter.drawPolygon(triangle)
        painter.end()

    def sizeHint(self) -> QSize:
        sh = self._button.sizeHint()
        return QSize(max(sh.width(), 220), max(sh.height(), 28))
