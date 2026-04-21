#!/usr/bin/env python3
"""Thin entry point for the SteelVoiceMix GUI.

The real code lives in the ``gui`` package next to this script. This shim
just ensures the script's directory is on sys.path so ``from gui.app``
resolves whether the launcher runs us from ~/.local/lib/steelvoicemix or
from a source checkout.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gui.app import main  # noqa: E402


if __name__ == "__main__":
    main()
