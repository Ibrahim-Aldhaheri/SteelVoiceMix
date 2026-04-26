"""Qt translation setup for SteelVoiceMix.

Loads compiled .qm translation files matching the system locale. Falls back
silently to English (the source language) when no matching translation exists.

To add a new language:
  1. Mark user-facing strings in code with `self.tr(...)` or `QObject.tr(...)`.
  2. Generate the .ts source: `pyside6-lupdate gui/*.py -ts gui/translations/<name>_<locale>.ts`
  3. Translate the .ts in Qt Linguist or any text editor.
  4. Compile to .qm: `pyside6-lrelease gui/translations/<name>_<locale>.ts`
  5. Ship the .qm file alongside the package.

The translator is a no-op when no .qm file matches — code that calls tr()
just gets the source-string back, which is fine.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QLocale, QTranslator
from PySide6.QtWidgets import QApplication

_TRANSLATIONS_DIR = Path(__file__).parent / "translations"


def setup_translator(app: QApplication) -> QTranslator | None:
    """Install a QTranslator for the current system locale. Returns the
    translator (kept alive by the caller) or None if nothing matched."""
    translator = QTranslator(app)
    locale = QLocale.system()
    # Try locale name (e.g. "steelvoicemix_fr_FR.qm") then language only ("steelvoicemix_fr.qm").
    candidates = [f"steelvoicemix_{locale.name()}", f"steelvoicemix_{locale.name().split('_')[0]}"]
    for candidate in candidates:
        if translator.load(candidate, str(_TRANSLATIONS_DIR)):
            app.installTranslator(translator)
            return translator
    return None
