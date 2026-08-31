"""Fonts for the window.

Two families, and the split between them is deliberate.

Inter (https://rsms.me/inter) carries the interface: menus, buttons, group
titles, tab labels, prose. It is bundled in fonts/ under the SIL Open Font
License, which fonts/OFL.txt reproduces, and it is loaded for this process
only. Nothing is installed and nothing outside this program can see it, so
a machine that runs the application once is left exactly as it was found.

Consolas carries the console and every numeric readout. Those are columns
of digits that change while you watch them. A proportional font makes the
columns move as the digits change width, and a number that jumps sideways
is a number that is hard to read at a glance. Monospace holds them still.
Do not "modernise" the readouts by moving them to Inter.

If Inter cannot be loaded, which is every platform except Windows unless
the user installed it, the interface falls back to the best system sans
face available. Nothing else changes.
"""

import sys
import tkinter.font as tkfont
from pathlib import Path

FONT_DIR = Path(__file__).resolve().parent / "fonts"

# Filenames registered with the system, in the order they are tried. Regular
# and Bold share the family "Inter"; SemiBold is its own family, because the
# RIBBI style model a classic font table can express has room for four faces
# and no more.
FONT_FILES = ("Inter-Regular.ttf", "Inter-Italic.ttf",
              "Inter-Bold.ttf", "Inter-SemiBold.ttf")

UI_FALLBACKS = ("Inter", "Segoe UI", "Helvetica Neue", "DejaVu Sans", "Arial")
SEMIBOLD_FALLBACKS = ("Inter SemiBold", "Inter", "Segoe UI Semibold",
                      "Segoe UI", "Helvetica Neue", "DejaVu Sans", "Arial")

# Not resolved against the system: the readouts are written for this face
# and have always used it. See the note at the top of this file.
MONO_FAMILY = "Consolas"

# Point sizes. Kept at the Windows shell default of 9 so the window keeps
# the density it was laid out with. Inter's x-height is larger than Segoe
# UI's, so the same point size already reads a little bigger; going up as
# well would push the left-hand panels past the minimum window width.
UI_SIZE = 9
UI_SIZE_SMALL = 8

# Cap height as a fraction of the hhea ascent, from Inter's own tables:
# 1490/2048 over 1984/2048. The About mark uses it to size the stripe square
# against the wordmark. Tk can report an ascent but cannot measure a glyph,
# so the ratio has to be carried here.
CAP_OVER_ASCENT = 0.751

_loaded = None


def load():
    """Register the bundled faces with this process. Idempotent.

    Returns the number of files the system accepted. Zero is not an error:
    it means the fallbacks in resolve() decide the family instead.

    Safe to call before or after the Tk root exists. Tk asks the system for
    a family each time a font is named, so a face registered late is still
    found.
    """
    global _loaded
    if _loaded is not None:
        return _loaded

    _loaded = 0
    if sys.platform != "win32":
        # Xft and CoreText have no equivalent of a process-private font that
        # can be added without touching the user's configuration.
        return _loaded

    try:
        import ctypes
        add = ctypes.windll.gdi32.AddFontResourceExW
    except Exception:
        return _loaded

    fr_private = 0x10
    for name in FONT_FILES:
        path = FONT_DIR / name
        if not path.exists():
            continue
        try:
            if add(ctypes.c_wchar_p(str(path)), fr_private, 0):
                _loaded += 1
        except Exception:
            pass
    return _loaded


def _first_present(root, names):
    have = {f.lower() for f in tkfont.families(root)}
    for name in names:
        if name.lower() in have:
            return name
    return names[-1]


def resolve(root):
    """Pick the families to use. Returns (ui, semibold, mono)."""
    load()
    return (_first_present(root, UI_FALLBACKS),
            _first_present(root, SEMIBOLD_FALLBACKS),
            MONO_FAMILY)


def apply_named_fonts(root, ui, mono):
    """Point Tk's named fonts at the chosen families.

    These are what a widget uses when it is given no font of its own, which
    is most of them. TkFixedFont is left on the monospace family so anything
    asking for a fixed font still gets one.
    """
    sizes = {
        "TkDefaultFont": UI_SIZE,
        "TkTextFont": UI_SIZE,
        "TkMenuFont": UI_SIZE,
        "TkHeadingFont": UI_SIZE,
        "TkIconFont": UI_SIZE,
        "TkCaptionFont": UI_SIZE,
        "TkSmallCaptionFont": UI_SIZE_SMALL,
        "TkTooltipFont": UI_SIZE_SMALL,
    }
    for name, size in sizes.items():
        try:
            tkfont.nametofont(name, root=root).configure(family=ui, size=size)
        except Exception:
            pass
    try:
        tkfont.nametofont("TkFixedFont", root=root).configure(family=mono,
                                                              size=UI_SIZE)
    except Exception:
        pass
