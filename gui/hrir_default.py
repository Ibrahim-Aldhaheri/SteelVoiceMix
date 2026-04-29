"""Fetch + cache the default HRIR for virtual surround.

ASM ships a single `hrir/EAC_Default.wav` file in their repo
(https://github.com/loteran/Arctis-Sound-Manager/tree/main/hrir) — a
generic HeSuVi-format 14-channel impulse response derived from
Equalizer APO Convolver's reference set. We don't bundle it (license
on the original IR is murky enough that redistribution gets tangled),
but the user can fetch it on demand via the Surround tab.

Cache layout: `$XDG_CACHE_HOME/steelvoicemix/hrir/EAC_Default.wav`.
The file is ~165 KB so re-downloading on demand isn't expensive, but
caching means the user doesn't pay the round-trip every launch.

Threading: same QThread + Signal pattern as the ASM preset importer
so the GUI can keep running while the file downloads.
"""

from __future__ import annotations

import logging
import os
import urllib.error
import urllib.request
from pathlib import Path

from PySide6.QtCore import QThread, Signal

log = logging.getLogger(__name__)

# Stable URL on ASM's main branch. If they ever rename / move the
# file, this will 404 cleanly and the importer surfaces the error.
DEFAULT_HRIR_URL = (
    "https://raw.githubusercontent.com/loteran/Arctis-Sound-Manager/"
    "main/hrir/EAC_Default.wav"
)
DEFAULT_HRIR_FILENAME = "EAC_Default.wav"
_HTTP_TIMEOUT = 12


def hrir_cache_dir() -> Path:
    """Where the cached default HRIR lives. Created on demand."""
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    d = Path(base) / "steelvoicemix" / "hrir"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cached_default_path() -> Path:
    """Absolute path the default HRIR will land at after a successful
    fetch. Caller can `.is_file()` to check whether the cache already
    exists before kicking off a download."""
    return hrir_cache_dir() / DEFAULT_HRIR_FILENAME


class DefaultHrirFetcher(QThread):
    """Download the default HRIR into the cache directory and emit the
    final path on success, or an error string on failure."""

    finished_with_path = Signal(str, str)  # (path, error)

    def run(self) -> None:
        target = cached_default_path()
        # If the file is already cached and non-empty, skip the download
        # — saves a round-trip on every "use default" click.
        if target.is_file() and target.stat().st_size > 0:
            self.finished_with_path.emit(str(target), "")
            return
        try:
            req = urllib.request.Request(
                DEFAULT_HRIR_URL,
                headers={"User-Agent": "steelvoicemix-gui/hrir-fetch"},
            )
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
                data = resp.read()
        except (urllib.error.URLError, OSError) as e:
            log.warning("HRIR fetch failed: %s", e)
            self.finished_with_path.emit("", f"Download failed: {e}")
            return
        if not data:
            self.finished_with_path.emit("", "Downloaded file was empty")
            return
        # Write atomically so a crash mid-download can't leave a
        # truncated WAV that the daemon would then try to load.
        tmp = target.with_suffix(".wav.tmp")
        try:
            tmp.write_bytes(data)
            tmp.replace(target)
        except OSError as e:
            log.warning("Could not save HRIR to %s: %s", target, e)
            self.finished_with_path.emit("", f"Could not save file: {e}")
            return
        self.finished_with_path.emit(str(target), "")
