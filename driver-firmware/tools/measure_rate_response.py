#!/usr/bin/env python3
"""Measure signal, noise, and end-pixel response at several frame rates.

Answers two questions with data rather than reasoning:

1. Why does frame rate change the apparent noise level?
2. Why is the first pixel high and the last pixel low?

Both are testable because they make opposite predictions under different
mechanisms, and frame rate is the variable that separates them.

Run with the detector under steady illumination and nothing else holding
the serial port:

    python tools/measure_rate_response.py

Takes about two minutes.

--- What the numbers mean ---

Exposure is set by the interval between START pulses, so exposure is
1/rate. That single fact drives most of what this script measures.

SIGNAL should scale with exposure. Doubling the interval doubles the
collected charge until the pixel saturates.

TEMPORAL NOISE, meaning the frame-to-frame standard deviation of one
pixel, separates two contributions:

  Readout noise is fixed per conversion. It does not care how long the
  pixel integrated, so in volts it stays constant as rate changes.

  Photon shot noise scales as the square root of collected charge. In
  volts it therefore grows as the square root of exposure, but signal
  grows linearly, so signal-to-noise still improves as the square root of
  exposure.

If noise in volts is flat across rate, readout noise dominates and the
apparent improvement at low rate is purely that the signal grew. If noise
in volts grows as roughly the square root of exposure, shot noise
dominates and the detector is performing as it should.

FIRST PIXEL is the diagnostic for the end-pixel question. If it is high
because it integrates the idle period between scans, which is much longer
than one 20 microsecond pixel period, then its excess over the body of the
array must grow as the idle time grows, meaning as rate falls. If the
excess is instead constant across rate, the cause is not integration time
and is more likely a readout or reset artifact fixed by the sensor's own
timing.

LAST PIXEL is checked the same way.
"""

import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from spectrometer import processing as P            # noqa: E402
from spectrometer.device import Device, find_devices  # noqa: E402

RATES = (2, 3, 5, 8, 12, 20, 30)
SECONDS_PER_RATE = 12
MIN_FRAMES = 8
BODY = slice(40, 980)      # array interior, away from both ends
EDGE_N = 8                 # pixels examined at each end


def collect(dev, info, hz, seconds):
    dev.set_rate(hz)
    time.sleep(1.0)
    dev.drain_frames(limit=100000)      # discard frames from the old rate
    dev.drain_stats(limit=100000)

    frames, t0 = [], time.time()
    want = max(MIN_FRAMES, int(hz * seconds * 0.6))
    while time.time() - t0 < seconds and len(frames) < want:
        frames.extend(dev.drain_frames())
        time.sleep(0.02)
    if len(frames) < MIN_FRAMES:
        return None

    stack = np.vstack([
        P.codes_to_volts(f.codes, info.vref_mv, info.full_scale)
        for f in frames])
    miss = sum(f.missing_count for f in frames)
    return {
        "hz": hz,
        "n": len(frames),
        "mean": np.nanmean(stack, axis=0),
        "sd": np.nanstd(stack, axis=0, ddof=1),
        "miss": miss,
    }


def main():
    ports = find_devices()
    if not ports:
        sys.exit("No serial ports found.")
    dev = Device()
    info = dev.connect(ports[0][0])
    if info.firmware == "unknown":
        dev.disconnect()
        sys.exit("Device did not identify. Is the firmware current?")

    lsb_mv = info.vref_mv / info.full_scale
    print(f"device   {info.board} fw {info.firmware}")
    print(f"detector {info.pixels} px, {info.bits}-bit, "
          f"{info.vref_mv} mV full scale")
    print(f"step     {lsb_mv * 1000:.2f} uV per code")
    print()

    rows = []
    try:
        for hz in RATES:
            r = collect(dev, info, hz, SECONDS_PER_RATE)
            if r is None:
                print(f"{hz:3d} Hz  insufficient frames, skipped")
                continue
            rows.append(r)
            body_sig = float(np.nanmean(r["mean"][BODY]))
            body_sd = float(np.nanmean(r["sd"][BODY]))
            print(f"{hz:3d} Hz  n={r['n']:4d}  "
                  f"signal {body_sig * 1000:8.2f} mV   "
                  f"noise {body_sd * 1000:7.4f} mV "
                  f"({body_sd * 1000 / lsb_mv:5.1f} codes)   "
                  f"miss {r['miss']}")
    finally:
        dev.set_rate(15)
        time.sleep(0.3)
        dev.disconnect()

    if len(rows) < 3:
        sys.exit("\nNot enough rates measured to draw a conclusion.")

    hz = np.array([r["hz"] for r in rows], dtype=float)
    exposure = 1.0 / hz
    signal = np.array([np.nanmean(r["mean"][BODY]) for r in rows])
    noise = np.array([np.nanmean(r["sd"][BODY]) for r in rows])

    print()
    print("=" * 68)
    print("SIGNAL versus EXPOSURE")
    print("=" * 68)
    print(_power_law(exposure, signal, "signal"))
    print("  exponent 1.0 means signal is proportional to exposure, which is")
    print("  the expected behaviour below saturation.")

    print()
    print("=" * 68)
    print("NOISE versus EXPOSURE")
    print("=" * 68)
    print(_power_law(exposure, noise, "noise"))
    print("  exponent near 0.0  -> readout noise dominates; noise in volts is")
    print("                        fixed, and low rate only looks quieter")
    print("                        because the signal is larger.")
    print("  exponent near 0.5  -> photon shot noise dominates; the detector")
    print("                        is performing as it should.")

    snr = signal / noise
    print()
    print("  rate    exposure   signal      noise      SNR")
    for i, r in enumerate(rows):
        print(f"  {r['hz']:3.0f} Hz  {exposure[i]*1000:7.1f} ms "
              f"{signal[i]*1000:9.2f} mV {noise[i]*1000:8.4f} mV "
              f"{snr[i]:8.1f}")

    print()
    print("=" * 68)
    print("END PIXELS")
    print("=" * 68)
    print("  Excess is the end pixel relative to the array interior. If the")
    print("  first pixel integrates the idle period between scans, its excess")
    print("  must grow as rate falls. A flat excess means something else.")
    print()
    print("  rate     px0        first8      body      last8       px1023")
    for r in rows:
        m = r["mean"]
        body = float(np.nanmedian(m[BODY]))
        print(f"  {r['hz']:3.0f} Hz  {m[0]*1000:8.2f}  {np.nanmean(m[:EDGE_N])*1000:9.2f}  "
              f"{body*1000:8.2f}  {np.nanmean(m[-EDGE_N:])*1000:9.2f}  "
              f"{m[-1]*1000:8.2f}   mV")

    body_all = np.array([np.nanmedian(r["mean"][BODY]) for r in rows])
    first_excess = np.array([r["mean"][0] for r in rows]) - body_all
    last_deficit = body_all - np.array([r["mean"][-1] for r in rows])

    print()
    print("  rate    first excess    last deficit")
    for i, r in enumerate(rows):
        print(f"  {r['hz']:3.0f} Hz  {first_excess[i]*1000:11.2f} mV "
              f"{last_deficit[i]*1000:12.2f} mV")

    print()
    print(_verdict("first pixel excess", exposure, first_excess))
    print(_verdict("last pixel deficit", exposure, last_deficit))


def _power_law(x, y, label):
    """Fit y = a * x**b and report b. Both quantities are positive and
    span a decade, so a log-log slope is the natural summary."""
    ok = np.isfinite(x) & np.isfinite(y) & (x > 0) & (y > 0)
    if np.count_nonzero(ok) < 3:
        return f"  {label}: not enough positive samples to fit"
    b, a = np.polyfit(np.log(x[ok]), np.log(y[ok]), 1)
    pred = np.exp(a) * x[ok] ** b
    resid = np.sqrt(np.mean((np.log(y[ok]) - np.log(pred)) ** 2))
    return (f"  {label} proportional to exposure^{b:.2f}  "
            f"(log residual {resid:.3f})")


def _verdict(label, exposure, excess):
    ok = np.isfinite(excess) & (excess > 0)
    if np.count_nonzero(ok) < 3:
        return f"  {label}: mostly non-positive, no integration dependence"
    b, _a = np.polyfit(np.log(exposure[ok]), np.log(excess[ok]), 1)
    spread = float(np.max(excess[ok]) / max(np.min(excess[ok]), 1e-12))
    if b > 0.5 and spread > 2.0:
        why = ("grows with exposure -> consistent with integrating the idle "
               "period between scans")
    elif spread < 1.5:
        why = ("flat across rate -> not an integration effect; a fixed "
               "readout or reset artifact")
    else:
        why = "weak or mixed dependence, inconclusive"
    return f"  {label}: exponent {b:+.2f}, spread {spread:.1f}x -> {why}"


if __name__ == "__main__":
    main()
