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
    the caller) or None if nothing matched.

    English (the source language) is intentionally a no-op — we
    don't ship steelvoicemix_en.qm because all source strings are
    already English. Likewise an explicit pick of an unavailable
    code installs no translator (caller can rely on the source
    strings) instead of silently falling back to the system locale."""
    code = _resolve_language(ui_language)
    if code == "en":
        return None
    translator = QTranslator(app)
    candidates = [f"steelvoicemix_{code}"]
    # Only consult the full system locale (e.g. 'ar_SA') as a
    # fallback when the user actually asked for the system language.
    # If they explicitly picked, say, 'en' or 'fr', we must NOT
    # re-introduce the system locale here — that's how an English
    # pick on an Arabic system kept loading Arabic.
    if ui_language == "system":
        candidates.append(f"steelvoicemix_{QLocale.system().name()}")
    for candidate in candidates:
        if translator.load(candidate, str(_TRANSLATIONS_DIR)):
            app.installTranslator(translator)
            return translator
    return None


def reset_translator(
    app: QApplication, ui_language: str,
) -> QTranslator | None:
    """Uninstall whatever translator is currently active on `app` and
    install one for `ui_language`. Used by the Settings → Language
    dropdown so a switch from Arabic to English actually drops the
    Arabic translator instead of layering English on top.

    Stores the live translator on `app._translator` so subsequent
    calls can find and remove it (matches the convention in app.py
    on first launch)."""
    existing = getattr(app, "_translator", None)
    if isinstance(existing, QTranslator):
        app.removeTranslator(existing)
    new_translator = setup_translator(app, ui_language)
    app._translator = new_translator
    return new_translator


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
