"""Persistent user settings for the SteelVoiceMix GUI.

Stored as JSON with a schema version so we can migrate cleanly when the
shape changes. A one-shot migration from the pre-v1 `settings.conf`
format lands new values into settings.json on first run.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

APP_NAME = "steelvoicemix"
DISPLAY_NAME = "SteelVoiceMix"
APP_VERSION = "0.2.2"

CONFIG_DIR = Path.home() / ".config" / APP_NAME
SETTINGS_FILE = CONFIG_DIR / "settings.json"
LEGACY_CONF = CONFIG_DIR / "settings.conf"

OVERLAY_POSITIONS = (
    "top-right",
    "top-left",
    "bottom-right",
    "bottom-left",
    "center",
)
OVERLAY_ORIENTATIONS = ("horizontal", "vertical")

SCHEMA_VERSION = 1

DEFAULTS: dict[str, Any] = {
    "schema": SCHEMA_VERSION,
    "overlay": True,
    "autostart": True,
    "overlay_position": "top-right",
    "overlay_orientation": "horizontal",
}


def socket_path() -> str:
    """Match the Rust daemon's socket location (XDG_RUNTIME_DIR preferred)."""
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return os.path.join(xdg, f"{APP_NAME}.sock")
    return f"/tmp/{APP_NAME}-{os.getuid()}.sock"


def _migrate_legacy() -> dict[str, Any] | None:
    """Parse a pre-schema settings.conf if present. Returns None if absent."""
    if not LEGACY_CONF.exists():
        return None
    try:
        result: dict[str, Any] = {}
        bool_keys = {"overlay", "autostart"}
        for line in LEGACY_CONF.read_text().strip().splitlines():
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            key, val = k.strip(), v.strip()
            if key in bool_keys:
                result[key] = val.lower() in ("true", "1", "yes")
            else:
                result[key] = val
        return result
    except Exception:
        return None


def load() -> dict[str, Any]:
    """Load settings, falling back to defaults. Migrates legacy conf on first run."""
    settings = dict(DEFAULTS)
    if SETTINGS_FILE.exists():
        try:
            loaded = json.loads(SETTINGS_FILE.read_text())
            if isinstance(loaded, dict):
                settings.update(loaded)
        except Exception:
            pass
    else:
        legacy = _migrate_legacy()
        if legacy:
            settings.update(legacy)
            save(settings)
            try:
                LEGACY_CONF.unlink()
            except OSError:
                pass
    settings["schema"] = SCHEMA_VERSION
    return settings


def save(settings: dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    serializable = {**settings, "schema": SCHEMA_VERSION}
    SETTINGS_FILE.write_text(json.dumps(serializable, indent=2) + "\n")


def normalize_position(value: str) -> str:
    return value if value in OVERLAY_POSITIONS else "top-right"


def normalize_orientation(value: str) -> str:
    return value if value in OVERLAY_ORIENTATIONS else "horizontal"
