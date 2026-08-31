"""Where the files that ship with the program live.

Two files are read from disk at startup rather than compiled in: the Inter
faces in fonts/, and the window icon. Run from source they sit next to this
module. Packaged by PyInstaller they are unpacked somewhere else, so asking
for the module's own directory would find nothing and the program would
start with the wrong font and no icon.

One place answers the question, so there is one thing to change if the
layout ever moves.
"""

import sys
from pathlib import Path

# The directory the package's data files are given inside a bundle. It has
# to match the destination in tools/make_exe.py.
BUNDLE_SUBDIR = "spectrometer"


def package_dir():
    """The directory holding fonts/ and the icons."""
    # PyInstaller sets this to the directory it unpacked the bundle into.
    # It is absent in a normal interpreter, which is how the two cases are
    # told apart: sys.frozen alone does not say where anything went.
    base = getattr(sys, "_MEIPASS", None)
    if base is not None:
        return Path(base) / BUNDLE_SUBDIR
    return Path(__file__).resolve().parent
