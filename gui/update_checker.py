"""Background check for newer SteelVoiceMix releases on GitHub.

Hits the GitHub Releases API once per startup (with a 24h on-disk cache so
repeated launches don't hammer the API), compares the latest tag to
APP_VERSION, and emits a Qt signal if a newer release is available. The
GUI surfaces it as a non-modal status hint — never a popup, never blocking.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from .settings import APP_NAME, APP_VERSION, CONFIG_DIR

_RELEASES_URL = "https://api.github.com/repos/Ibrahim-Aldhaheri/SteelVoiceMix/releases/latest"
_TAGS_URL = "https://api.github.com/repos/Ibrahim-Aldhaheri/SteelVoiceMix/tags?per_page=20"
_CACHE_FILE = CONFIG_DIR / "update-cache.json"
_CACHE_TTL_S = 24 * 60 * 60
_REQUEST_TIMEOUT_S = 5


def _parse_version(tag: str) -> tuple[int, ...] | None:
    """Parse 'v0.2.4' / '0.2.4' / 'v0.2.4-rc1' into (0, 2, 4). Returns None
    if the tag doesn't look like a numeric version."""
    if not tag:
        return None
    s = tag.lstrip("v").split("-", 1)[0]
    parts = s.split(".")
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return None


def _read_cache() -> dict | None:
    try:
        if not _CACHE_FILE.exists():
            return None
        data = json.loads(_CACHE_FILE.read_text())
        if time.time() - data.get("fetched_at", 0) > _CACHE_TTL_S:
            return None
        return data
    except Exception:
        return None


def _write_cache(latest_tag: str) -> None:
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(
            json.dumps({"fetched_at": time.time(), "latest_tag": latest_tag})
        )
    except Exception:
        pass


def _http_get_json(url: str):
    """Single GET that raises on network failure but returns None on 404
    (so callers can distinguish 'endpoint has no data' from 'offline')."""
    req = urllib.request.Request(
        url, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def _fetch_latest_tag() -> str | None:
    """Find the newest version tag on the upstream repo. Try the GitHub
    Releases API first (richer metadata when releases exist), fall back to
    the tags endpoint for repos that just push tags without cutting a
    Release object — which is exactly our workflow.

    Returns the tag string, or None if nothing reachable looks like a
    version. Raises on actual network errors so the worker can distinguish
    'no release found' from 'offline'."""
    rel = _http_get_json(_RELEASES_URL)
    if rel is not None:
        tag = rel.get("tag_name")
        if tag:
            return tag

    tags = _http_get_json(_TAGS_URL)
    if not isinstance(tags, list):
        return None
    # Pick the newest version-like tag (highest semver) — repo may have
    # other tags (e.g. 'flathub-rebase') we want to ignore.
    versioned: list[tuple[tuple[int, ...], str]] = []
    for entry in tags:
        name = entry.get("name", "") if isinstance(entry, dict) else ""
        v = _parse_version(name)
        if v is not None:
            versioned.append((v, name))
    if not versioned:
        return None
    versioned.sort(reverse=True)
    return versioned[0][1]


class _CheckerWorker(QObject):
    """Runs the network call on its own thread so the GUI never blocks."""

    update_available = Signal(str, str)  # latest_tag, current_version
    no_update = Signal()
    no_release_found = Signal()           # reachable but nothing tagged yet
    failed = Signal()                     # actual network / parse error

    def run(self) -> None:
        # Cache check first — stays local if we polled within the last day.
        cached = _read_cache()
        latest: str | None = None
        if cached is not None:
            latest = cached.get("latest_tag")
        else:
            try:
                latest = _fetch_latest_tag()
            except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                # Truly offline / DNS / connection refused. Distinguish from
                # "endpoint reachable but no version tag found".
                self.failed.emit()
                return
            if latest is not None:
                _write_cache(latest)

        if latest is None:
            # Reachable upstream but no tagged release exists.
            self.no_release_found.emit()
            return

        latest_v = _parse_version(latest)
        current_v = _parse_version(APP_VERSION)
        if latest_v is None or current_v is None:
            self.no_release_found.emit()
            return

        if latest_v > current_v:
            self.update_available.emit(latest, APP_VERSION)
        else:
            self.no_update.emit()


class UpdateChecker(QObject):
    """Public façade: keeps the worker + thread alive and re-emits signals.

    Caller wires:
      checker = UpdateChecker(parent)
      checker.update_available.connect(on_update_available)
      checker.start()
    """

    update_available = Signal(str, str)
    no_update = Signal()
    no_release_found = Signal()
    failed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread: QThread | None = None
        self._worker: _CheckerWorker | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = QThread(self)
        self._worker = _CheckerWorker()
        self._worker.moveToThread(self._thread)
        self._worker.update_available.connect(self.update_available.emit)
        self._worker.no_update.connect(self.no_update.emit)
        self._worker.no_release_found.connect(self.no_release_found.emit)
        self._worker.failed.connect(self.failed.emit)
        self._thread.started.connect(self._worker.run)
        # Tear down the thread when worker emits any terminal signal.
        for sig in (
            self._worker.update_available,
            self._worker.no_update,
            self._worker.no_release_found,
            self._worker.failed,
        ):
            sig.connect(self._thread.quit)
        self._thread.start()

    def force_check(self) -> None:
        """Bypass the cache and re-check (e.g. for a 'Check now' button)."""
        try:
            if _CACHE_FILE.exists():
                _CACHE_FILE.unlink()
        except OSError:
            pass
        self._thread = None  # let start() create a fresh worker/thread
        self.start()
