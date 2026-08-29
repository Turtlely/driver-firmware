#!/usr/bin/env python3
"""Launch the spectrometer application from the tools directory."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from spectrometer.app import main  # noqa: E402

if __name__ == "__main__":
    main()
