"""About / disclaimer dialog.

Surfaces the SteelSeries non-affiliation notice inside the app itself
— a requirement for Flathub submission and generally good practice for
a reverse-engineered compatibility tool.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QVBoxLayout,
)

from .settings import APP_VERSION, DISPLAY_NAME

HOMEPAGE = "https://github.com/Ibrahim-Aldhaheri/SteelVoiceMix"
ISSUES_URL = "https://github.com/Ibrahim-Aldhaheri/SteelVoiceMix/issues"


def make_about_dialog(parent=None) -> QDialog:
    dialog = QDialog(parent)
    dialog.setWindowTitle(f"About {DISPLAY_NAME}")
    dialog.setWindowIcon(
        QIcon.fromTheme("steelvoicemix", QIcon.fromTheme("audio-headset"))
    )
    dialog.setMinimumWidth(420)

    layout = QVBoxLayout(dialog)
    layout.setSpacing(10)
    layout.setContentsMargins(20, 20, 20, 16)

    title = QLabel(f"<h2>{DISPLAY_NAME}</h2>")
    title.setAlignment(Qt.AlignCenter)
    layout.addWidget(title)

    version = QLabel(f"Version {APP_VERSION}")
    version.setAlignment(Qt.AlignCenter)
    version.setStyleSheet("color: #888;")
    layout.addWidget(version)

    summary = QLabel(
        "ChatMix for the SteelSeries Arctis Nova Pro Wireless on Linux. "
        "Creates virtual PipeWire sinks controlled by the hardware dial on "
        "the base station."
    )
    summary.setWordWrap(True)
    layout.addWidget(summary)

    disclaimer = QLabel(
        "<b>Disclaimer:</b> SteelVoiceMix has no affiliation with SteelSeries. "
        "The author is not responsible for damage, bricked devices, voided "
        "warranties, or any other outcome from using this software. Use at "
        "your own risk."
    )
    disclaimer.setWordWrap(True)
    disclaimer.setStyleSheet(
        "background: rgba(255, 193, 7, 0.15);"
        "border-radius: 6px;"
        "padding: 10px;"
    )
    layout.addWidget(disclaimer)

    links = QLabel(
        f'<p style="text-align:center;">'
        f'<a href="{HOMEPAGE}">Homepage</a>'
        f' &nbsp;·&nbsp; '
        f'<a href="{ISSUES_URL}">Report an issue</a>'
        f'</p>'
    )
    links.setAlignment(Qt.AlignCenter)
    links.setOpenExternalLinks(True)
    layout.addWidget(links)

    license_lbl = QLabel("Licensed under the MIT License")
    license_lbl.setAlignment(Qt.AlignCenter)
    license_lbl.setStyleSheet("color: #888; font-size: 11px;")
    layout.addWidget(license_lbl)

    buttons = QDialogButtonBox(QDialogButtonBox.Close)
    buttons.rejected.connect(dialog.reject)
    buttons.accepted.connect(dialog.accept)
    layout.addWidget(buttons)

    return dialog
