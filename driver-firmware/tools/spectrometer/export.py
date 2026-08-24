"""Spectrum export.

Written as commented tab-separated text so a saved spectrum is readable in
a text editor, loads directly with numpy.loadtxt or pandas.read_csv, and
carries enough header to be interpreted years later without the software
that produced it.

Acquisition conditions go in the header for the same reason. A spectrum
without its integration time, reference-correction state and calibration is
not reproducible, and those are exactly the details that are forgotten
first.
"""

import time

import numpy as np


def write_spectrum(path, pixels, volts, *, wavelengths=None, info=None,
                   frames=1, elapsed_s=0.0, dark_applied=False,
                   flat_applied=False, notch_applied=False,
                   calibration=None, peaks=None, note=""):
    lines = []
    a = lines.append

    a("# S3903-1024Q spectrum")
    a(f"# saved:            {time.strftime('%Y-%m-%d %H:%M:%S')}")
    if note:
        a(f"# note:             {note}")
    if info is not None:
        a(f"# device:           {info.board} firmware {info.firmware}")
        a(f"# detector:         {info.pixels} px, {info.bits}-bit, "
          f"{info.vref_mv} mV reference")
        a(f"# frame_rate_hz:    {info.rate_hz}")
    a(f"# frames_combined:  {frames}")
    a(f"# integration_s:    {elapsed_s:.3f}")
    a(f"# dark_subtracted:  {'yes' if dark_applied else 'no'}")
    a(f"# flat_corrected:   {'yes' if flat_applied else 'no'}")
    a(f"# pattern_notched:  {'yes, 17.68 kHz' if notch_applied else 'no'}")
    a(f"# calibration:      {calibration.describe() if calibration else 'none'}")
    if calibration is not None and calibration.valid:
        pts = ", ".join(f"{p:.1f}px={w:.3f}nm" for p, w in calibration.points)
        a(f"# calib_points:     {pts}")

    valid = np.isfinite(volts)
    a(f"# valid_pixels:     {int(np.count_nonzero(valid))} of {len(volts)}")
    a("# missing pixels are written as NaN, not zero: no trigger arrived")

    if peaks:
        a("#")
        a(f"# detected peaks ({len(peaks)}):")
        for i, pk in enumerate(peaks, 1):
            centre = pk["centre"]
            if wavelengths is not None:
                wl = float(np.interp(centre, np.arange(len(wavelengths)),
                                     wavelengths))
                a(f"#   {i}: centre={centre:.2f}px ({wl:.3f}nm) "
                  f"height={pk['height']:.6f}V "
                  f"prominence={pk['prominence']:.6f}V "
                  f"fwhm={pk['width']:.1f}px")
            else:
                a(f"#   {i}: centre={centre:.2f}px "
                  f"height={pk['height']:.6f}V "
                  f"prominence={pk['prominence']:.6f}V "
                  f"fwhm={pk['width']:.1f}px")

    a("#")
    if wavelengths is not None:
        a("# columns: pixel\twavelength_nm\tvolts\trelative")
    else:
        a("# columns: pixel\tvolts\trelative")

    finite = volts[np.isfinite(volts)]
    peak = float(np.max(finite)) if finite.size else 1.0
    if peak <= 0:
        peak = 1.0

    for i, p in enumerate(pixels):
        v = volts[i]
        rel = v / peak
        vs = "NaN" if not np.isfinite(v) else f"{v:.6f}"
        rs = "NaN" if not np.isfinite(rel) else f"{rel:.6f}"
        if wavelengths is not None:
            a(f"{int(p)}\t{wavelengths[i]:.4f}\t{vs}\t{rs}")
        else:
            a(f"{int(p)}\t{vs}\t{rs}")

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def write_reference(path, volts):
    """Dark or flat reference. Plain .npy: these are intermediate data
    consumed only by this application, so a binary format that preserves
    NaN exactly is preferable to text."""
    np.save(path, np.asarray(volts, dtype=np.float64))
    return path


def read_reference(path, expected_pixels):
    data = np.load(path)
    if data.ndim != 1 or data.shape[0] != expected_pixels:
        raise ValueError(
            f"reference has {data.shape} values, expected {expected_pixels}")
    return data.astype(np.float64)
