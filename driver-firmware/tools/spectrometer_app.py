#!/usr/bin/env python3
"""Launcher for the spectrometer application.

    python tools/spectrometer_app.py

Kept as a plain script at the top of tools/ so the application starts the
same way regardless of whether the package directory is on sys.path.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from spectrometer.app import main  # noqa: E402

if __name__ == "__main__":
    main()
