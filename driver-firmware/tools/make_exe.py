r"""Package the application as a Windows executable.

    python tools/make_exe.py              one folder, fast to start
    python tools/make_exe.py --onefile    one .exe, slow to start
    python tools/make_exe.py --clean      discard the cached analysis first

Needs PyInstaller, which is a build-time tool and not a runtime dependency:

    .venv\Scripts\python -m pip install pyinstaller

Output goes to build/exe/. The folder build is dist/Helios/ and its entry
point is dist/Helios/Helios.exe; the one-file build is dist/Helios.exe.

**Which to ship.** Measured on the development machine, the folder build
puts a window on screen in 0.3 s and the one-file build takes 2.2 s, every
launch. The one-file build unpacks numpy, tkinter and the Tcl library into
a temporary directory before it can start, and it pays that cost again on
every run rather than once. Two seconds of nothing looks like a program
that has failed to open. Prefer the folder, and zip it to hand it over.
The folder is 63 MB and the single file is 26 MB.

**Why a script and not a .spec file.** A .spec is Python that PyInstaller
executes, so it can hold this logic, but it is also a file that has to be
kept in step with the tree by hand. Here the data files are listed once,
next to the reason each one is needed.
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
PKG = TOOLS / "spectrometer"

NAME = "Helios"
ENTRY = TOOLS / "spectrometer_app.py"
ICON = PKG / "icon.ico"

# Everything the program opens at runtime rather than imports. Each entry is
# (source, destination inside the bundle). The destination has to agree with
# BUNDLE_SUBDIR in tools/spectrometer/resources.py, which is what the
# program uses to find these again once it is packaged.
DATA = [
    # The interface font. Loaded from disk with AddFontResourceEx, so it
    # cannot be compiled in; see tools/spectrometer/typography.py.
    (PKG / "fonts", "spectrometer/fonts"),
    # Title bar and taskbar. The .ico carries every size Windows asks for.
    (ICON, "spectrometer"),
    # The icon for platforms without .ico support, kept so the packaged
    # program behaves the same way the source tree does.
    (PKG / "logo.png", "spectrometer"),
]

# PyInstaller finds imports by reading the source. It cannot see the
# sys.path entry that tools/spectrometer_app.py adds at runtime, so the
# directory holding the package has to be named here or the whole package
# is left out of the bundle.
PATHS = [TOOLS]

WORK = ROOT / "build" / "exe"


def build(onefile, clean):
    if not ENTRY.exists():
        raise SystemExit(f"Entry point not found: {ENTRY}")
    if not ICON.exists():
        raise SystemExit(f"Icon not found: {ICON} (run: python tools/make_logo.py)")
    for src, _ in DATA:
        if not src.exists():
            raise SystemExit(f"Missing bundled file: {src}")

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--name", NAME,
        # No console window behind the interface. This is what pythonw.exe
        # does for the source tree, so the packaged program matches it.
        "--windowed",
        "--icon", str(ICON),
        "--distpath", str(WORK / "dist"),
        "--workpath", str(WORK / "work"),
        "--specpath", str(WORK),
    ]
    cmd.append("--onefile" if onefile else "--onedir")
    if clean:
        cmd.append("--clean")
    for path in PATHS:
        cmd += ["--paths", str(path)]
    for src, dest in DATA:
        cmd += ["--add-data", f"{src}{os_sep()}{dest}"]
    cmd.append(str(ENTRY))

    print(" ".join(cmd))
    subprocess.run(cmd, check=True, cwd=ROOT)

    out = WORK / "dist" / (f"{NAME}.exe" if onefile else NAME)
    print()
    print(f"built {out}")
    if not onefile:
        print(f"  run {out / (NAME + '.exe')}")
    print(f"  size {size_of(out) / 1e6:.1f} MB")


def os_sep():
    """The separator --add-data wants between source and destination."""
    return ";" if sys.platform == "win32" else ":"


def size_of(path):
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--onefile", action="store_true",
                    help="one .exe instead of a folder; slower to start")
    ap.add_argument("--clean", action="store_true",
                    help="discard PyInstaller's cached analysis first")
    args = ap.parse_args()

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        raise SystemExit("PyInstaller is not installed:\n"
                         f"    {sys.executable} -m pip install pyinstaller")

    build(args.onefile, args.clean)


if __name__ == "__main__":
    main()
