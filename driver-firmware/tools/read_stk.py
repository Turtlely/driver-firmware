"""Read .STK files straight off a memory card, without the instrument.

    python tools/read_stk.py E:/SPECTRA                 list every file
    python tools/read_stk.py E:/SPECTRA/SP000007.STK    one file, in detail
    python tools/read_stk.py E:/SPECTRA -o out_folder   write spectra as text

A stored file is compressed by default, so a card reader alone will not show
you anything useful. This is the decoder. It reads both forms a card can
hold: the container written by firmware 1.3 and later, and the plain record
written before that.

Nothing here talks to a device. It exists so that data on a card is never
hostage to having the instrument, the application, or this program's author.
"""
import argparse
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from spectrometer import export, processing  # noqa: E402
from spectrometer.stkfile import (BadFile, CODEC_NAMES,  # noqa: E402
                                  PIXELS, decode_pixels, parse_record,
                                  read_record)

# The decoder lives in spectrometer/stkfile.py so that this program and the
# application read a card file through the same code. The names above are
# re-exported here because this file is also the documented entry point.
__all__ = ["BadFile", "decode_pixels", "read_record", "parse_record",
           "describe", "main"]


class _Stack:
    """The attribute names export.write_spectrum expects."""

    def __init__(self, d):
        self.__dict__.update(d)


class _Info:
    board = "rpipico2"
    firmware = "from card"
    pixels = PIXELS
    bits = 14
    vref_mv = 2500
    rate_hz = 0


def describe(path, verbose):
    try:
        record, codec, stored = read_record(path)
    except BadFile as exc:
        print(f"{Path(path).name:<14} UNREADABLE: {exc}")
        return None
    d = parse_record(record)
    ratio = len(record) / stored
    name = CODEC_NAMES.get(codec, str(codec))
    # From the record first, then the name prefix the firmware writes.
    kind = {"SP": "spectrum", "DK": "dark", "FL": "flat"}.get(
        Path(path).name[:2].upper(), "spectrum")
    if d["dark"]:
        kind = "dark"
    print(f"{Path(path).name:<14} id {d['capture_id']:<5} "
          f"{d['frames_stacked']:>5} frames  "
          f"{d['exposure_us'] / 1000.0:>8.2f} ms  "
          f"{stored:>6} B {name:<4} {ratio:4.2f}x  {kind}")
    if verbose:
        dead = int(np.count_nonzero(d["accepted"] <= 0))
        kept = int(d["accepted"].sum())
        cut = int(d["rejected"].sum())
        print(f"    requested {d['frames_requested']}, attempted "
              f"{d['frames_attempted']}, excluded {d['frames_excluded']}")
        print(f"    clip {d['sigma']:.2f} sigma, {d['clip_iters']} iterations, "
              f"blocks of {d['block_frames']}")
        print(f"    samples kept {kept}, clipped {cut}, "
              f"pixels with no data {dead}")
        print(f"    device counters: missing {d['stat_missing']}  "
              f"stale {d['stat_stale']}  badbits {d['stat_badbits']}  "
              f"worst late {d['stat_max_late_us']} us  "
              f"nostart {d['stat_nostart']}")
    return d


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", help="a .STK file, or a folder of them")
    ap.add_argument("-o", "--out", help="write each spectrum here as text")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="print the capture counters as well")
    args = ap.parse_args()

    target = Path(args.path)
    if target.is_dir():
        files = sorted(p for p in target.iterdir()
                       if p.suffix.upper() == ".STK")
        if not files:
            print(f"no .STK files in {target}")
            return 1
    else:
        files = [target]
        args.verbose = True

    out_dir = Path(args.out) if args.out else None
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)

    bad = 0
    for f in files:
        d = describe(f, args.verbose)
        if d is None:
            bad += 1
            continue
        if out_dir is None:
            continue
        codes = np.array(d["mean"], dtype=np.float64)
        codes[d["accepted"] <= 0] = np.nan
        # The same conversion the application uses, imported rather than
        # repeated, so a file written here and one saved from the window
        # cannot disagree about what a code is worth.
        volts = processing.codes_to_volts(codes, _Info.vref_mv,
                                          1 << _Info.bits)
        dest = out_dir / (f.stem + ".txt")
        export.write_spectrum(
            dest, np.arange(len(volts)), volts, info=_Info(),
            frames=d["frames_stacked"], elapsed_s=d["duration_ms"] / 1000.0,
            stack=_Stack(d), note=f"card_file:{f.name}")
        print(f"    wrote {dest}")

    if bad:
        print(f"\n{bad} of {len(files)} files could not be read")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
