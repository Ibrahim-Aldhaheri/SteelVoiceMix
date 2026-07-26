"""Shared fixtures for the offscreen GUI tests.

These tests construct the real widgets with the daemon client and update
checker stubbed, so they exercise layout and interaction logic without a
running daemon, a display, or a headset. Everything runs under the Qt
'offscreen' platform plugin, so it works headless in CI.
"""
from __future__ import annotations

import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("XDG_RUNTIME_DIR", "/tmp/steelvoicemix-test-runtime")
os.makedirs(os.environ["XDG_RUNTIME_DIR"], exist_ok=True)
# Keep test config out of the real user config dir.
os.environ.setdefault(
    "XDG_CONFIG_HOME", "/tmp/steelvoicemix-test-config"
)
os.makedirs(os.environ["XDG_CONFIG_HOME"], exist_ok=True)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


@pytest.fixture
def stub_daemon(monkeypatch):
    """Neuter the daemon client so no socket is opened, and record the
    commands the GUI tries to send so tests can assert on them."""
    import gui.daemon_client as dc

    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(dc.DaemonClient, "run", lambda self: None)
    monkeypatch.setattr(
        dc.DaemonClient,
        "send_command",
        lambda self, cmd, **kw: sent.append((cmd, kw)),
    )
    monkeypatch.setattr(dc.DaemonClient, "stop", lambda self: None)

    import gui.update_checker as uc
    monkeypatch.setattr(uc.UpdateChecker, "start", lambda self, *a, **k: None)
    return sent


@pytest.fixture
def window(qapp, stub_daemon):
    """A fully-built MixerGUI with the daemon stubbed. Returns
    (window, sent_commands)."""
    from gui.main_window import MixerGUI
    from gui.theme import apply_theme

    apply_theme("dark")
    win = MixerGUI()
    win.show()
    qapp.processEvents()
    yield win, stub_daemon
    win.close()
    qapp.processEvents()


def pump(qapp, n: int = 3) -> None:
    for _ in range(n):
        qapp.processEvents()
