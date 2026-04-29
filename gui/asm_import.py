"""Background fetch of the ASM preset library, run from the EQ tab.

Lives in its own module because:
  - It uses Qt threading primitives, which we want isolated from the
    pure-data `eq_presets` module so headless tests can still import
    that without pulling PySide6.
  - Network failures, JSON-decode failures, and per-file rejections
    are all logged and surfaced as a single completion signal — no
    half-finished imports left behind.

The fetch flow:
  1. GET the GitHub contents API for the presets folder (one HTTP call).
  2. For each file with a `[Game]` / `[Chat]` tag, GET the raw file.
  3. Convert the Sonar shape via `eq_presets.convert_sonar_preset`.
  4. Save under the user preset dir as `<name>.json` with an "[ASM]"
     prefix so the EQ combo can show provenance at a glance.

The whole import runs in a QThread so the GUI stays responsive — 400+
HTTP GETs takes ~30 seconds on a typical connection. The thread emits
progress at every file so the dialog can show a percentage.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request

from PySide6.QtCore import QThread, Signal

from .eq_presets import (
    ASM_PRESETS_API,
    ASM_PRESETS_RAW,
    NUM_BANDS,
    _ASM_TAG_TO_CHANNEL,
    convert_sonar_preset,
    save_user_preset,
)

log = logging.getLogger(__name__)

# Filename prefix on disk so the EQ combo can flag ASM-imported presets
# distinct from the user's own `Custom N` saves and the built-ins.
ASM_NAME_PREFIX = "[ASM] "

# Conservative HTTP timeout — slow connections still complete, but we
# don't hang the import thread if GitHub is misbehaving.
_HTTP_TIMEOUT = 12

# Pattern to extract `[Tag]` from filenames like 'Apex Legends [Game].json'.
_TAG_RE = re.compile(r"\[([^\]]+)\]")


def _http_get_json(url: str) -> object:
    """Single GET that decodes JSON. Raises on any failure — the caller
    catches and surfaces a friendly error."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "steelvoicemix-gui/asm-import"},
    )
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


class AsmPresetImporter(QThread):
    """Background importer. Emits `progress(done, total, label)` every
    file processed and `finished_with_summary(saved, skipped, error)`
    once at the end (`error` is non-empty only if the listing fetch
    failed, in which case `saved` and `skipped` are zero)."""

    progress = Signal(int, int, str)
    finished_with_summary = Signal(int, int, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cancel = False

    def cancel(self) -> None:
        """Ask the importer to stop after the next file. Safe to call
        from the GUI thread."""
        self._cancel = True

    def run(self) -> None:
        try:
            listing = _http_get_json(ASM_PRESETS_API)
        except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
            log.warning("ASM preset listing fetch failed: %s", e)
            self.finished_with_summary.emit(0, 0, f"Listing fetch failed: {e}")
            return
        if not isinstance(listing, list):
            self.finished_with_summary.emit(
                0, 0, "Unexpected listing shape from GitHub"
            )
            return

        # Filter to the tags we know how to import + skip directories.
        candidates: list[tuple[str, str, str]] = []  # (filename, name, channel)
        for entry in listing:
            if not isinstance(entry, dict) or entry.get("type") != "file":
                continue
            filename = str(entry.get("name", ""))
            if not filename.endswith(".json"):
                continue
            tag_match = _TAG_RE.search(filename)
            if not tag_match:
                continue
            channel = _ASM_TAG_TO_CHANNEL.get(tag_match.group(1))
            if channel is None:
                # Drop [Mic] and any unknown tags — we don't have a
                # mic-side EQ chain yet.
                continue
            # Strip the [Tag].json suffix for the user-visible name.
            base = filename[: tag_match.start()].strip()
            if base.endswith(" "):
                base = base.rstrip()
            candidates.append((filename, base, channel))

        total = len(candidates)
        if total == 0:
            self.finished_with_summary.emit(0, 0, "No usable presets found")
            return

        saved = 0
        skipped = 0
        for done, (filename, name, channel) in enumerate(candidates, start=1):
            if self._cancel:
                break
            display_name = f"{ASM_NAME_PREFIX}{name}"
            self.progress.emit(done, total, name)

            url = ASM_PRESETS_RAW + urllib.parse.quote(filename)
            try:
                payload = _http_get_json(url)
            except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
                log.warning("Skipping %s: fetch failed: %s", filename, e)
                skipped += 1
                continue
            if not isinstance(payload, dict):
                skipped += 1
                continue

            bands = convert_sonar_preset(payload)
            if bands is None or len(bands) != NUM_BANDS:
                skipped += 1
                continue

            try:
                save_user_preset(display_name, channel, bands)
                saved += 1
            except (ValueError, OSError) as e:
                log.warning("Could not save %s: %s", display_name, e)
                skipped += 1

        self.finished_with_summary.emit(saved, skipped, "")


# urlencoding lives in urllib.parse; importing at module level keeps
# the QThread.run path free of import overhead.
import urllib.parse  # noqa: E402
