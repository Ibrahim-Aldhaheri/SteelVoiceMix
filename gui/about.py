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
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtWidgets import QApplication as _QApp
    _tr = lambda s: QCoreApplication.translate("AboutDialog", s)
    dialog = QDialog(parent)
    dialog.setWindowTitle(_tr("About {app}").format(app=DISPLAY_NAME))
    dialog.setWindowIcon(
        QIcon.fromTheme("steelvoicemix", QIcon.fromTheme("audio-headset"))
    )
    # Inherit the application's layoutDirection (RTL on Arabic etc.).
    # Without this, child dialogs default to LTR even when the main
    # window is RTL — Qt applies layoutDirection at app-level but
    # standalone QDialogs created post-init don't pick it up unless
    # asked explicitly.
    app = _QApp.instance()
    if app is not None:
        dialog.setLayoutDirection(app.layoutDirection())
    dialog.setMinimumWidth(420)

    layout = QVBoxLayout(dialog)
    layout.setSpacing(10)
    layout.setContentsMargins(20, 20, 20, 16)

    title = QLabel(f"<h2>{DISPLAY_NAME}</h2>")
    title.setAlignment(Qt.AlignCenter)
    layout.addWidget(title)

    version = QLabel(_tr("Version {ver}").format(ver=APP_VERSION))
    version.setAlignment(Qt.AlignCenter)
    version.setStyleSheet("color: #888;")
    layout.addWidget(version)

    summary = QLabel(
        _tr(
            "ChatMix for the SteelSeries Arctis Nova Pro Wireless on Linux. "
            "Creates virtual PipeWire sinks controlled by the hardware dial on "
            "the base station."
        )
    )
    summary.setWordWrap(True)
    layout.addWidget(summary)

    disclaimer = QLabel(
        _tr(
            "<b>Disclaimer:</b> SteelVoiceMix has no affiliation with SteelSeries. "
            "The author is not responsible for damage, bricked devices, voided "
            "warranties, or any other outcome from using this software. Use at "
            "your own risk."
        )
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
        f'<a href="{HOMEPAGE}">{_tr("Homepage")}</a>'
        f' &nbsp;·&nbsp; '
        f'<a href="{ISSUES_URL}">{_tr("Report an issue")}</a>'
        f'</p>'
    )
    links.setAlignment(Qt.AlignCenter)
    links.setOpenExternalLinks(True)
    layout.addWidget(links)

    license_lbl = QLabel(_tr("Licensed under the GNU GPL-3.0-only"))
    license_lbl.setAlignment(Qt.AlignCenter)
    license_lbl.setStyleSheet("color: palette(placeholder-text); font-size: 11px;")
    layout.addWidget(license_lbl)

    buttons = QDialogButtonBox(QDialogButtonBox.Close)
    buttons.rejected.connect(dialog.reject)
    buttons.accepted.connect(dialog.accept)
    layout.addWidget(buttons)

    return dialog
