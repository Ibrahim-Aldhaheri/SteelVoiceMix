"""Qt translation setup for SteelVoiceMix.

Loads compiled .qm translation files matching either the user's
explicit choice (`ui_language` setting) or the system locale.
Falls back silently to English (the source language) when no
matching translation exists.

Adding / improving a language:
  1. Mark user-facing strings in code with `self.tr(...)` or
     `QObject.tr(...)`. Coverage is currently partial — many
     strings still hardcoded in English.
  2. Generate the .ts source:
        pyside6-lupdate gui/**/*.py -ts gui/translations/<code>.ts
  3. Translate in Qt Linguist or any text editor.
  4. Compile to .qm:
        pyside6-lrelease gui/translations/<code>.ts
  5. Ship the .qm alongside the package.

The translator is a no-op when no .qm matches — code that calls
tr() just gets the source string back.

Right-to-left layout: `apply_layout_direction` flips the Qt
application's layoutDirection when the active language is in
_RTL_LANGUAGES (Arabic, Hebrew, Farsi, Urdu).
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QLocale, Qt, QTranslator
from PySide6.QtWidgets import QApplication

_TRANSLATIONS_DIR = Path(__file__).parent / "translations"

# Languages exposed in the Settings dropdown. 'system' is added at
# index 0 by the Settings tab itself; this list drives the rest.
SUPPORTED_LANGUAGES: tuple[tuple[str, str], ...] = (
    ("en", "English"),
    ("ar", "العربية"),
)

# Language codes that read right-to-left. Drives apply_layout_direction.
_RTL_LANGUAGES: frozenset = frozenset({"ar", "he", "fa", "ur"})


def _resolve_language(setting: str) -> str:
    """Map the saved `ui_language` setting (may be 'system' or an
    explicit code) to a concrete two-letter language code."""
    if setting and setting != "system":
        return setting
    return QLocale.system().name().split("_")[0]


def setup_translator(
    app: QApplication, ui_language: str = "system",
) -> QTranslator | None:
    """Install a QTranslator for the chosen language. `ui_language`
    is the saved setting; 'system' uses QLocale.system(), anything
    else is a literal code. Returns the translator (kept alive by
    the caller) or None if nothing matched."""
    translator = QTranslator(app)
    code = _resolve_language(ui_language)
    candidates = (
        f"steelvoicemix_{code}",
        # Fall back to full locale (e.g. 'fr_FR') when only a
        # regional variant has been compiled.
        f"steelvoicemix_{QLocale.system().name()}",
    )
    for candidate in candidates:
        if translator.load(candidate, str(_TRANSLATIONS_DIR)):
            app.installTranslator(translator)
            return translator
    return None


def apply_layout_direction(
    app: QApplication, ui_language: str = "system",
) -> None:
    """Flip the application's layoutDirection based on the chosen
    language. Called at startup after setup_translator and again
    whenever the user changes the Settings → Appearance language
    dropdown."""
    code = _resolve_language(ui_language)
    if code in _RTL_LANGUAGES:
        app.setLayoutDirection(Qt.RightToLeft)
    else:
        app.setLayoutDirection(Qt.LeftToRight)
