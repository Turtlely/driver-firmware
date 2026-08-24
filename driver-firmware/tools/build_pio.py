"""Regenerate PIO headers before every firmware build.

pixel_trig.pio.h is a generated file. Without this script it is generated
by hand, which means editing pixel_trig.pio and forgetting to re-run
pioasm produces firmware that compiles cleanly, flashes cleanly, and
silently contains the PREVIOUS version of the PIO program. No build
warning, no link error, no runtime complaint. The sibling mainboard
project documented losing real debugging time to exactly this, testing
timing theories against a binary that never contained the edits.

PlatformIO's Arduino builder for RP2040/RP2350 has no equivalent of the
Pico SDK's pico_generate_pio_header() CMake rule, so this script supplies
it: platformio.ini registers it as a pre-build action, and it re-runs
pioasm for any .pio file whose source is newer than its generated header.

The generated headers stay checked in, so a build still works if this
script cannot locate pioasm for some reason - it warns rather than failing
the build in that case, since a stale-but-present header is still
compilable. main.cpp's printPioBuildStamp() prints a checksum of the
instruction words actually linked into the binary, which is the runtime
confirmation that what is running matches what is on disk.
"""

import os
import subprocess
import sys

Import("env")  # noqa: F821 - injected by PlatformIO's script runner

PROJECT_DIR = env.subst("$PROJECT_DIR")  # noqa: F821
SRC_DIR = os.path.join(PROJECT_DIR, "src")


def find_pioasm():
    """Locate the pioasm executable inside the installed PlatformIO packages."""
    packages_dir = env.subst("$PROJECT_PACKAGES_DIR")  # noqa: F821
    candidates = [
        os.path.join(packages_dir, "tool-pioasm-rp2040-earlephilhower", "pioasm.exe"),
        os.path.join(packages_dir, "tool-pioasm-rp2040-earlephilhower", "pioasm"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def regenerate():
    pio_sources = [f for f in os.listdir(SRC_DIR) if f.endswith(".pio")]
    if not pio_sources:
        return

    pioasm = find_pioasm()
    if pioasm is None:
        print("WARNING: pioasm not found; .pio headers NOT regenerated. "
              "If you edited a .pio file, the build will silently use the "
              "previous version. Regenerate by hand before trusting results.")
        return

    for name in pio_sources:
        src = os.path.join(SRC_DIR, name)
        header = src + ".h"

        if os.path.isfile(header) and os.path.getmtime(header) >= os.path.getmtime(src):
            continue  # header is already newer than its source

        print("Regenerating %s from %s" % (os.path.basename(header), name))
        result = subprocess.run([pioasm, src, header],
                                capture_output=True, text=True)
        if result.returncode != 0:
            # A .pio syntax error must fail the build. Letting it through
            # would leave the old header in place and produce firmware
            # quietly running the previous program, which is the exact
            # failure this script exists to prevent.
            sys.stderr.write(result.stderr)
            env.Exit(1)  # noqa: F821


regenerate()
