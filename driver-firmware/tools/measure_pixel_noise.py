#!/usr/bin/env python3
"""Measure the distribution and spectrum of selected pixels over time.

Written after a pixel's distribution histogram in the application showed
two lobes with a dip near the mean, not the single central peak that
ordinary readout noise should produce.

That specific shape, peaks near both extremes and a trough in the middle,
is the signature of an arcsine ("bathtub") distribution: sampling a
slowly-varying periodic signal at essentially random phase relative to its
own period. A sine wave spends most of its time near its extremes and
moves fastest through the middle, so samples taken at unrelated times land
near the extremes more often than near the middle. Thermal and amplifier
noise, being a sum of many small independent contributions, is expected to
be Gaussian by the central limit theorem instead: one peak, centred on the
mean, falling off symmetrically.

This script pulls a raw per-frame time series for chosen pixels (not the
displayed, averaged trace) and reports the statistics that separate the
two possibilities:

  Excess kurtosis. Gaussian is 0. An arcsine/bathtub shape is negative,
  around -1.5; a uniform distribution is also negative, around -1.2.
  Positive excess kurtosis means the opposite failure: a sharp central
  peak with heavy tails, e.g. mostly-good readings plus rare large
  misreads, which is a different problem and would not look bimodal.

  Autocorrelation at short lags. A near-random alternation between two
  states shows up as negative lag-1 autocorrelation. A slow periodic
  interferer instead shows a smoothly decaying, oscillating
  autocorrelation over several lags.

  A coarse FFT of the raw series, to see whether a dominant frequency is
  present at all and roughly where it sits.

  The same statistics over several pixels spread across the array, to
  tell a global effect (every pixel affected together) from something
  local to one pixel, one region, or one ADC channel.

Run with the detector under steady, stable illumination and nothing else
holding the serial port:

    python tools/measure_pixel_noise.py [pixel ...]

With no arguments, samples seven pixels spread across the array. Takes
about 15 seconds.
"""

import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from spectrometer import processing as P            # noqa: E402
from spectrometer.device import Device, find_devices  # noqa: E402

RATE_HZ = 20
SECONDS = 15
LAGS = 5
FFT_TOP_N = 3


def collect(dev, info, seconds):
    dev.set_rate(RATE_HZ)
    time.sleep(1.0)
    dev.drain_frames(limit=100000)      # discard frames from the old rate

    frames, t0 = [], time.time()
    while time.time() - t0 < seconds:
        frames.extend(dev.drain_frames())
        time.sleep(0.02)
    if not frames:
        return np.empty((0, info.pixels))
    return np.vstack([
        P.codes_to_volts(f.codes, info.vref_mv, info.full_scale)
        for f in frames])


def skewness(x):
    x = x - np.mean(x)
    m2 = np.mean(x ** 2)
    if m2 <= 0:
        return np.nan
    return float(np.mean(x ** 3) / m2 ** 1.5)


def excess_kurtosis(x):
    """Fisher's definition: 0 for Gaussian, negative for flat-topped or
    bimodal shapes, positive for a sharp peak with heavy tails."""
    x = x - np.mean(x)
    m2 = np.mean(x ** 2)
    if m2 <= 0:
        return np.nan
    return float(np.mean(x ** 4) / m2 ** 2 - 3.0)


def autocorr(x, max_lag):
    x = x - np.mean(x)
    var = float(np.dot(x, x))
    if var <= 0:
        return np.full(max_lag, np.nan)
    return np.array([np.dot(x[:-lag], x[lag:]) / var
                      for lag in range(1, max_lag + 1)])


def dominant_freqs(x, fs, top_n):
    x = x - np.mean(x)
    n = x.size
    if n < 8:
        return []
    spec = np.abs(np.fft.rfft(x * np.hanning(n)))
    freqs = np.fft.rfftfreq(n, d=1.0 / fs)
    spec[0] = 0.0  # exclude DC
    order = np.argsort(spec)[::-1][:top_n]
    return [(float(freqs[k]), float(spec[k])) for k in order if spec[k] > 0]


def common_mode_correlation(stack, pixels, exclude_head=P.MASK_HEAD,
                             exclude_tail=P.MASK_TAIL):
    """Correlate each pixel's frame-to-frame deviation against the average
    deviation of many pixels spread across the array in the same frame.

    Separates two explanations for a bimodal, non-periodic, frame-memoryless
    per-pixel distribution: a disturbance common to the whole readout during
    that frame (supply, reference, or ground bounce while every pixel is
    converted), versus something local to each pixel's own conversion (a
    marginal comparator decision, timing jitter on that one channel's
    bit-banged SPI transaction). A common-mode driver moves many pixels
    together within a frame; a per-channel effect does not, so this is
    checked as a within-frame cross-pixel correlation, not the frame-to-frame
    correlation report_pixel() already checks per pixel.
    """
    n_frames, n_px = stack.shape
    lo, hi = exclude_head, n_px - exclude_tail
    if hi - lo < 16:
        return None
    step = max(1, (hi - lo) // 128)
    sample = np.arange(lo, hi, step)
    sub = stack[:, sample]
    valid = np.all(np.isfinite(sub), axis=1)
    sub = sub[valid]
    if sub.shape[0] < 20:
        return None

    dev = sub - np.mean(sub, axis=0, keepdims=True)
    common = np.mean(dev, axis=1)  # per-frame average deviation, ~128 pixels
    if np.std(common) <= 0:
        return None

    out = {}
    for px in pixels:
        if not (0 <= px < n_px):
            continue
        series = stack[valid, px]
        d = series - np.mean(series)
        out[px] = (float(np.corrcoef(d, common)[0, 1])
                   if np.std(d) > 0 else np.nan)
    return out


def step_size_correlation(stack, pixels, exclude_tail=P.MASK_TAIL):
    """Correlate which of a pixel's two states a frame lands in against the
    size of the step from the immediately preceding pixel, in that same
    frame.

    All 1024 pixels are read from one shared video line, one after another.
    A pixel following a large jump from its neighbour asks more of that
    line's settling than one following a small jump. If the two-state
    effect is a settling race, the state should correlate with step size;
    if it is unrelated (independent per-conversion timing jitter, or
    something internal to the ADC itself), it should not.

    Returns {pixel: correlation}, using state = 0/1 by which side of the
    pixel's own two-cluster midpoint each frame's reading falls on, so
    this works directly from the same stack already collected for
    report_pixel(), no extra acquisition needed.
    """
    n_frames, n_px = stack.shape
    out = {}
    for px in pixels:
        if not (1 <= px < n_px - exclude_tail):
            out[px] = None
            continue
        cur, prev = stack[:, px], stack[:, px - 1]
        valid = np.isfinite(cur) & np.isfinite(prev)
        if np.count_nonzero(valid) < 20:
            out[px] = None
            continue
        cur_v, step = cur[valid], np.abs(cur[valid] - prev[valid])
        split = two_cluster_split(cur_v)
        if split is None:
            out[px] = None
            continue
        lo, hi, _lo_m, _hi_m = split
        state = (cur_v > (lo + hi) / 2.0).astype(np.float64)
        if np.std(state) <= 0 or np.std(step) <= 0:
            out[px] = None
            continue
        out[px] = float(np.corrcoef(state, step)[0, 1])
    return out


def two_cluster_split(values):
    """Split a 1D sample into two clusters by Lloyd's algorithm (k-means,
    k=2), seeded at the population min and max.

    Added to check a specific number rather than a shape: if a pixel's
    bimodal distribution is two levels differing by a clean, fixed amount
    in raw ADC codes, that points at something quantised, e.g. a bit
    boundary or a fixed offset. A difference that is not a round number
    in codes, or that does not divide evenly by anything meaningful,
    argues against a clean digital cause and for something continuously
    variable instead (e.g. an analogue disturbance of varying size).

    Returns (low_centre, high_centre, low_members, high_members), or None
    if there is not enough data or it does not actually separate into two
    clusters. Membership is returned, not just counts, so a caller can look
    at the exact modal code within each cluster rather than its mean.
    """
    x = np.asarray(values, dtype=np.float64)
    if x.size < 20:
        return None
    c0, c1 = float(np.min(x)), float(np.max(x))
    if c1 <= c0:
        return None
    for _ in range(50):
        d0 = np.abs(x - c0)
        d1 = np.abs(x - c1)
        g0 = x[d0 <= d1]
        g1 = x[d0 > d1]
        if g0.size == 0 or g1.size == 0:
            return None
        nc0, nc1 = float(np.mean(g0)), float(np.mean(g1))
        if nc0 == c0 and nc1 == c1:
            break
        c0, c1 = nc0, nc1
    else:
        # Exhausted the iteration budget without converging; recompute
        # membership once more against the final centres so what is
        # returned is never stale relative to them.
        d0 = np.abs(x - c0)
        d1 = np.abs(x - c1)
        g0 = x[d0 <= d1]
        g1 = x[d0 > d1]
        if g0.size == 0 or g1.size == 0:
            return None
    if c0 <= c1:
        return float(c0), float(c1), g0, g1
    return float(c1), float(c0), g1, g0


def modal_code(values):
    """Most frequent integer value in a sample, after rounding.

    A cluster's mean can land off the true quantised value once it is
    averaged over noisy samples; the mode instead reports what the
    hardware actually returned most often, which is what matters when
    comparing two states bit for bit."""
    if values.size == 0:
        return None
    ints = np.round(values).astype(np.int64)
    vals, counts = np.unique(ints, return_counts=True)
    return int(vals[np.argmax(counts)])


def shape_hint(kurt):
    if not np.isfinite(kurt):
        return "not enough spread to judge"
    if kurt < -0.5:
        return "flat-topped / bimodal (arcsine-like) -> consider periodic interference"
    if kurt > 1.0:
        return "sharp peak, heavy tails -> consider occasional misreads"
    return "roughly Gaussian"


def report_pixel(idx, series, fs, lsb_mv):
    finite = series[np.isfinite(series)]
    n = finite.size
    if n < 20:
        print(f"pixel {idx:4d}: only {n} valid samples, skipped")
        return
    mv = finite * 1000.0
    mean = float(np.mean(mv))
    std = float(np.std(mv, ddof=1))
    skew = skewness(mv)
    kurt = excess_kurtosis(mv)
    ac = autocorr(mv, LAGS)
    top = dominant_freqs(finite, fs, FFT_TOP_N)

    print(f"pixel {idx:4d}  n={n:4d}  mean={mean:9.3f} mV  "
          f"std={std:7.4f} mV ({std / lsb_mv:5.1f} codes)")
    print(f"           skew={skew:+.3f}  excess kurtosis={kurt:+.3f}  "
          f"-> {shape_hint(kurt)}")
    print("           lag1.." + str(LAGS) + " autocorr: "
          + " ".join(f"{v:+.2f}" for v in ac))
    if top:
        print("           strongest FFT components (Hz @ rel.amp): "
              + ", ".join(f"{f:.2f}@{a:.0f}" for f, a in top))
    if kurt < -0.5:
        codes = mv / lsb_mv
        split = two_cluster_split(codes)
        if split:
            lo, hi, lo_members, hi_members = split
            n_lo, n_hi = lo_members.size, hi_members.size
            total = n_lo + n_hi
            diff = hi - lo
            print(f"           two-level split: {lo:.1f} / {hi:.1f} codes  "
                  f"(delta {diff:.1f} codes, {diff * lsb_mv:.3f} mV)  "
                  f"n={n_lo}/{n_hi} ({100 * n_lo / total:.0f}%/"
                  f"{100 * n_hi / total:.0f}%)")

            lo_mode = modal_code(lo_members)
            hi_mode = modal_code(hi_members)
            if lo_mode is not None and hi_mode is not None and lo_mode != hi_mode:
                xor = lo_mode ^ hi_mode
                bits = [i for i in range(14) if xor & (1 << i)]
                print(f"           modal codes: {lo_mode} vs {hi_mode}  "
                      f"(0b{lo_mode:014b} vs 0b{hi_mode:014b})")
                print(f"           bit(s) that differ: {bits}  "
                      f"(XOR 0b{xor:014b} = {xor})")
    print()


def main():
    requested = [int(a) for a in sys.argv[1:]]

    ports = find_devices()
    if not ports:
        sys.exit("No serial ports found.")
    dev = Device()
    info = dev.connect(ports[0][0])
    if info.firmware == "unknown":
        dev.disconnect()
        sys.exit("Device did not identify. Is the firmware current?")

    lsb_mv = info.vref_mv / info.full_scale
    n = info.pixels
    pixels = requested or sorted(set((
        2, n // 8, n // 4, n // 2, 3 * n // 4, 7 * n // 8, n - 3)))

    print(f"device   {info.board} fw {info.firmware}")
    print(f"detector {info.pixels} px, {info.bits}-bit, "
          f"{info.vref_mv} mV full scale, step {lsb_mv * 1000:.2f} uV/code")
    print(f"rate     {RATE_HZ} Hz, collecting {SECONDS} s")
    print(f"pixels   {pixels}")
    print()

    try:
        stack = collect(dev, info, SECONDS)
    finally:
        dev.disconnect()

    if stack.shape[0] < 20:
        sys.exit(f"Only {stack.shape[0]} frames collected, not enough. "
                  f"Check illumination is steady and the port is free.")

    print(f"collected {stack.shape[0]} frames")
    print()
    for px in pixels:
        report_pixel(px, stack[:, px], RATE_HZ, lsb_mv)

    print("=" * 68)
    print("CROSS-PIXEL CORRELATION (common-mode check)")
    print("=" * 68)
    print("Correlation between each pixel's own frame-to-frame deviation")
    print("and the average deviation of ~128 pixels spread across the")
    print("array, in the SAME frame. This is a different question from the")
    print("lag autocorrelation above (which looks across frames at one")
    print("pixel): this looks across pixels within one frame.")
    print()
    corr = common_mode_correlation(stack, pixels)
    if corr is None:
        print("  not enough frames with every sampled pixel valid to check")
    else:
        for px in pixels:
            r = corr.get(px, np.nan)
            if not np.isfinite(r):
                tag = "n/a"
            elif abs(r) > 0.3:
                tag = "-> moves with the rest of the array (common-mode)"
            else:
                tag = "-> independent of the rest of the array (per-channel)"
            print(f"  pixel {px:4d}  r={r:+.3f}  {tag}")
    print()

    print("=" * 68)
    print("STEP-SIZE CORRELATION (video-line settling check)")
    print("=" * 68)
    print("Correlation between which of a pixel's two states a frame lands")
    print("in and the size of the jump from the pixel immediately before it")
    print("in the SAME frame. All 1024 pixels share one video line read out")
    print("serially, so a bigger jump asks more of that line's settling")
    print("before this pixel's conversion. A real correlation here means")
    print("the two-state effect tracks how far the line had to move, not")
    print("just whether it moved at all.")
    print()
    step_corr = step_size_correlation(stack, pixels)
    for px in pixels:
        r = step_corr.get(px)
        if r is None or not np.isfinite(r):
            print(f"  pixel {px:4d}  n/a")
            continue
        tag = ("-> state tracks step size (settling-related)" if abs(r) > 0.3
               else "-> state does not track step size")
        print(f"  pixel {px:4d}  r={r:+.3f}  {tag}")
    print()

    print("=" * 68)
    print("Reading all of this together:")
    print("  Excess kurtosis near 0 with flat lag autocorrelation is")
    print("  ordinary Gaussian readout noise: no further action.")
    print()
    print("  Negative excess kurtosis (flat-topped or two lobes with a dip")
    print("  at the mean) PLUS a real, consistent FFT line PLUS smoothly")
    print("  decaying lag autocorrelation means a periodic interferer")
    print("  sampled at random phase (supply ripple, a clock beating")
    print("  against the frame rate).")
    print()
    print("  Negative excess kurtosis with flat lag autocorrelation and no")
    print("  consistent FFT line instead means two-state random switching,")
    print("  decorrelated frame to frame: each conversion independently")
    print("  lands near one of two levels rather than drawing from a single")
    print("  continuous distribution. The cross-pixel correlation then")
    print("  decides where: high |r| on many pixels means a per-frame")
    print("  disturbance common to the whole readout (supply, reference, or")
    print("  ground bounce during the conversion burst); |r| near 0 on every")
    print("  pixel rules that out and means each conversion is independent.")
    print()
    print("  A consistent DECIMAL code delta across pixels that does NOT")
    print("  correspond to a fixed bit pattern is not a contradiction: a")
    print("  fixed arithmetic offset added to different base values produces")
    print("  different bit patterns through ordinary binary carry, so this")
    print("  combination (steady delta, scattered XOR) is itself evidence")
    print("  for a roughly constant ANALOGUE offset landing on some")
    print("  conversions, not a specific bit or comparator decision.")
    print()
    print("  The step-size correlation then tests where that offset comes")
    print("  from. A real correlation (|r| notably above 0) means the state")
    print("  tracks how far the shared video line had to move before this")
    print("  pixel's conversion, consistent with a settling race on that")
    print("  line. |r| near 0 across every pixel argues against that and")
    print("  points at something internal to the ADC's own conversion (e.g.")
    print("  aperture jitter), independent of what the signal is doing.")


if __name__ == "__main__":
    main()
