"""Frame processing: accumulation, reference correction, statistics.

The processing order is fixed and matters:

    raw codes -> volts -> dark subtraction -> flat field -> accumulation

Dark subtraction happens on raw volts, before any gain or display scaling,
because the dark frame is itself measured in raw volts. Flat fielding
follows because it corrects per-pixel gain, which only has meaning once the
additive offset is removed.

Missing pixels are propagated as NaN rather than zero throughout. Zero is a
legal reading and would silently bias every statistic that touched it; NaN
forces every consumer to handle the gap explicitly, and numpy's nan-aware
functions then exclude them from means, sums and extrema by construction.
"""

import numpy as np

from .device import MAX_CODE


def codes_to_volts(codes, vref_mv, full_scale):
    """Raw codes to volts, with missing pixels as NaN."""
    codes = np.asarray(codes)
    out = codes.astype(np.float64) * (vref_mv / 1000.0) / full_scale
    out[codes > MAX_CODE] = np.nan
    return out


# Pixels at each end that do not carry a valid measurement.
#
# Established by measurement, not assumption. Sweeping the frame rate from
# 2 to 30 Hz, with the array interior spanning 307 to 1592 mV:
#
#   Pixel 1023 held 202 to 217 mV at every unsaturated rate, varying by
#   under 5 mV while the interior varied by a factor of three. A pixel that
#   does not respond to exposure is not measuring light. It reads the video
#   line baseline, most likely because the final trigger samples after the
#   last element has been shifted out.
#
#   Pixel 0 read 121 percent of the interior against 110 percent for pixel
#   1, an excess of about 11 percent over its immediate neighbour that does
#   not scale with exposure. That is a readout artifact at the start of the
#   shift register, distinct from the broad illumination gradient across
#   the first couple of hundred pixels, which is real signal and is what a
#   flat field is for.
#
# Masked rather than deleted so pixel indices continue to match the
# detector, and set to NaN so every downstream statistic excludes them by
# construction rather than by each caller remembering to.
MASK_HEAD = 1
MASK_TAIL = 1


def mask_end_pixels(volts, head=MASK_HEAD, tail=MASK_TAIL):
    out = np.array(volts, dtype=np.float64, copy=True)
    if head:
        out[:head] = np.nan
    if tail:
        out[len(out) - tail:] = np.nan
    return out


# Spatial frequency band, in cycles per pixel, occupied by the fixed
# pattern interference characterised in ISSUES.md entry 31.
#
# Measured on this instrument: a single sinusoidal component at 0.3539
# cycles per pixel, a period of 2.825 pixels, which at the CPLD's 20
# microsecond pixel period is 17.68 kHz in real time. It is additive
# (amplitude 47 codes regardless of whether the pixel sits at 2780 or
# 2345 codes), asynchronous to the readout (50 kHz / 17.68 kHz = 2.825,
# not an integer, so its phase is uniformly random from frame to frame),
# and stable to 0.1 percent across captures minutes apart and at
# different frame rates. It accounts for about three quarters of the
# apparent per-pixel noise variance.
#
# The second band is its aliased second harmonic, at about 11 percent of
# the fundamental. 2 x 0.3539 = 0.7078 exceeds the 0.5 cycles per pixel
# Nyquist limit of the pixel array and folds back to 1 - 0.7078 = 0.2922.
#
# Both bands are wider than the observed drift, by roughly an order of
# magnitude, so a frequency that wanders slightly is still caught.
PATTERN_BANDS = ((0.335, 0.375), (0.283, 0.302))


def notch_pattern(volts, bands=PATTERN_BANDS):
    """Remove fixed pattern interference by notching its spatial frequency.

    This is a correction for a characterised instrument artifact, in the
    same category as dark subtraction, not a general purpose smoother. It
    attacks one narrow, measured, non-physical frequency band and leaves
    everything else alone.

    Safe because the interference and the signal do not overlap in spatial
    frequency. Real spectral structure is broad: on this instrument 99.96
    percent of the time-averaged frame's energy lies below 0.30 cycles per
    pixel, since an emission line spans several pixels and the
    illumination envelope spans hundreds. The interference is a spike at
    0.354. Measured effect of this function on synthetic emission lines
    added to real frames: a 3 pixel FWHM line retains 98.7 percent of its
    peak and 100 percent of its area, and 5 pixel and wider lines retain
    100 percent of both.

    Measured effect on noise, over 399 frames: per-pixel temporal standard
    deviation falls from 39.8 to 8.0 codes across the array interior, a
    factor of 5, and the value distribution of each pixel returns from
    arcsine (excess kurtosis -1.3) to Gaussian (about 0). No pixel is made
    worse.

    The outer 15 or so pixels at each end improve by about a factor of 2
    rather than 5. The frame is not periodic, so the interference has an
    arbitrary phase discontinuity at each end that a Fourier method cannot
    represent exactly. That residue is random per frame, unlike the
    impulse artifact described under `guard`, so it does average away:
    what survives a 399 frame stack is 0.57 mV peak, against a 1.2 mV
    single frame floor.

    Mirror padding was tried and made the edges worse, because the
    reflected copy breaks the sinusoid's phase at the seam. A per-frame
    least squares sinusoid fit, which makes no periodicity assumption, was
    also tried: it handles the edges better (17 against 25 codes) but the
    interior worse (11 against 8), because the interference is a narrow
    band rather than one exact frequency. Narrowing the bands was tried
    and rejected: it cut the surviving artifact from 0.57 to 0.52 mV while
    raising interior noise from 1.22 to 1.38 mV.

    Missing pixels are interpolated across before the transform, since a
    NaN would otherwise propagate to every output sample, then restored
    afterwards so nothing is invented.

    The end pixels are excluded from the transform and passed through
    unchanged, for the reason in `guard`.
    """
    x = np.asarray(volts, dtype=np.float64)
    n = x.size
    if n < 32:
        return x.copy()

    finite = np.isfinite(x)
    if not finite.any():
        return x.copy()

    # Guard the ends before anything else.
    #
    # Pixels 0 and 1023 are not measurements (see mask_end_pixels): pixel
    # 0 sits about 86 mV above its neighbour and pixel 1023 about 199 mV
    # below. To a Fourier method those are two large impulses, and
    # notching an impulse spreads it into a decaying oscillation running
    # inward from each end. That oscillation is set by the static frame
    # shape rather than by the interferer's random phase, so it is
    # identical in every frame and does not average away: it was measured
    # at 23 mV peak, surviving a 399 frame stack, against a corrected
    # noise floor of 1.2 mV.
    #
    # Handled here rather than by requiring the caller to mask first, so
    # that the result does not depend on the order corrections are
    # applied in, and so that unmasking the end pixels for inspection
    # cannot silently reintroduce the artifact. Guarded pixels are
    # returned exactly as they came in, filtered or not.
    guard = np.zeros(n, dtype=bool)
    if MASK_HEAD:
        guard[:MASK_HEAD] = True
    if MASK_TAIL:
        guard[n - MASK_TAIL:] = True
    usable = finite & ~guard
    if usable.sum() < 32:
        return x.copy()

    idx = np.arange(n)
    work = x.copy()
    fill = ~usable
    work[fill] = np.interp(idx[fill], idx[usable], x[usable])

    # Subtract a baseline that meets the data at both ends, not the least
    # squares line. The transform treats the frame as one period of a
    # periodic signal, so any level difference between the first and last
    # sample is a step at the wrap point, and its broadband content leaks
    # into the notched bands as more edge ringing. The illumination
    # profile is curved, so a least squares line leaves tens of millivolts
    # of mismatch; anchoring the baseline to the ends removes it. Measured
    # effect on the surviving stacked artifact: 1.04 mV to 0.57 mV.
    #
    # The anchors are medians of the outermost usable pixels rather than
    # single samples, so one noisy pixel cannot tilt the baseline.
    edge = max(1, min(8, (n - MASK_HEAD - MASK_TAIL) // 8))
    lo_v = float(np.median(work[MASK_HEAD:MASK_HEAD + edge]))
    hi_v = float(np.median(work[n - MASK_TAIL - edge:n - MASK_TAIL]))
    t = idx.astype(np.float64)
    base = lo_v + (hi_v - lo_v) * t / (n - 1)

    spectrum = np.fft.rfft(work - base)
    freqs = np.fft.rfftfreq(n, d=1.0)
    for lo, hi in bands:
        spectrum[(freqs >= lo) & (freqs <= hi)] = 0.0
    out = np.fft.irfft(spectrum, n) + base

    out[guard] = x[guard]
    out[~finite] = np.nan
    return out


def apply_dark(volts, dark):
    if dark is None:
        return volts
    return volts - dark


def apply_flat(volts, flat):
    """Divide by a normalised response map to remove per-pixel gain
    variation. The map is normalised to a median of 1.0 at capture time, so
    this neither adds nor removes overall signal level. Non-finite results,
    which occur where the flat is at or near zero, are returned as NaN so
    they are treated as missing rather than as a large spurious value."""
    if flat is None:
        return volts
    with np.errstate(divide="ignore", invalid="ignore"):
        out = volts / flat
    out[~np.isfinite(out)] = np.nan
    return out


class Accumulator:
    """Running mean over the most recent N frames.

    Kept as a running sum with a NaN-aware per-pixel count, so a pixel that
    is missing in some frames is still averaged correctly over the frames
    where it was present. A plain mean over a fixed frame count would bias
    those pixels toward zero in proportion to how often they dropped out.
    """

    def __init__(self, pixels, maxlen=1):
        self.pixels = pixels
        self.maxlen = max(1, int(maxlen))
        self._frames = []

    def resize(self, maxlen):
        self.maxlen = max(1, int(maxlen))
        if len(self._frames) > self.maxlen:
            del self._frames[:len(self._frames) - self.maxlen]

    def clear(self):
        self._frames.clear()

    def push(self, volts):
        self._frames.append(np.asarray(volts, dtype=np.float64))
        if len(self._frames) > self.maxlen:
            self._frames.pop(0)

    @property
    def depth(self):
        return len(self._frames)

    def mean(self):
        if not self._frames:
            return np.full(self.pixels, np.nan)
        stack = np.vstack(self._frames)
        with np.errstate(invalid="ignore"):
            return np.nanmean(stack, axis=0)

    def std(self):
        """Per-pixel standard deviation across the accumulated frames.
        Needs at least two frames to be meaningful."""
        if len(self._frames) < 2:
            return np.full(self.pixels, np.nan)
        stack = np.vstack(self._frames)
        with np.errstate(invalid="ignore"):
            return np.nanstd(stack, axis=0, ddof=1)


def sigma_clip_combine(frames, sigma=3.0, iters=5, method="median"):
    """Combine frames, rejecting per-pixel outliers.

    A plain median already tolerates a single bad sample, but a run of
    consecutive bad frames can still drag it. Iterative sigma clipping
    identifies outliers explicitly against a median and standard deviation
    that are themselves re-estimated from the surviving samples, which
    handles clustered corruption that rank alone does not.

    Use median for reference frames, where conservatism matters most, and
    mean for signal, where averaging the survivors gives better noise
    reduction once outliers are gone.
    """
    stack = np.vstack([np.asarray(f, dtype=np.float64) for f in frames])
    mask = np.isfinite(stack)

    for _ in range(int(iters)):
        masked = np.where(mask, stack, np.nan)
        with np.errstate(invalid="ignore"):
            med = np.nanmedian(masked, axis=0)
            std = np.nanstd(masked, axis=0)
        std = np.where(np.isfinite(std) & (std > 0), std, np.inf)
        new_mask = mask & (np.abs(stack - med) <= sigma * std)
        if np.array_equal(new_mask, mask):
            break
        mask = new_mask

    masked = np.where(mask, stack, np.nan)
    with np.errstate(invalid="ignore"):
        return (np.nanmedian(masked, axis=0) if method == "median"
                else np.nanmean(masked, axis=0))


def normalise_flat(volts, clip_low=0.05):
    """Turn a bright-reference exposure into a gain map with median 1.0.

    Pixels below clip_low of the median are forced to NaN. Those are pixels
    that received almost no light, where dividing by the map would amplify
    noise without bound rather than correct anything.
    """
    volts = np.asarray(volts, dtype=np.float64)
    with np.errstate(invalid="ignore"):
        med = np.nanmedian(volts)
    if not np.isfinite(med) or med <= 0:
        return None
    flat = volts / med
    flat[~np.isfinite(flat)] = np.nan
    flat[flat < clip_low] = np.nan
    return flat


def statistics(volts, region=None):
    """Summary statistics over one frame, missing pixels excluded.

    `std` here is the spread ACROSS PIXELS within the frame. On a real
    spectrum that is dominated by spectral structure and the illumination
    profile, which are signal, not noise. It is reported as spatial spread
    and must not be used as a noise figure.

    Noise requires repeated measurement of the same pixel, which one frame
    cannot provide. Use snr_from_noise() with a temporal standard
    deviation from Accumulator.std().
    """
    v = np.asarray(volts, dtype=np.float64)
    if region is not None:
        lo, hi = region
        v = v[max(0, lo):min(len(v), hi)]
    finite = v[np.isfinite(v)]
    if finite.size == 0:
        return {"n": 0, "valid": 0, "min": np.nan, "max": np.nan,
                "mean": np.nan, "median": np.nan, "spatial_std": np.nan,
                "p2p": np.nan}
    return {
        "n": int(v.size),
        "valid": int(finite.size),
        "min": float(np.min(finite)),
        "max": float(np.max(finite)),
        "mean": float(np.mean(finite)),
        "median": float(np.median(finite)),
        "spatial_std": (float(np.std(finite, ddof=1))
                        if finite.size > 1 else 0.0),
        "p2p": float(np.max(finite) - np.min(finite)),
    }


def snr_from_noise(signal_volts, noise_volts, baseline_volts=0.0):
    """Signal-to-noise from a signal level and a temporal noise level.

    Both arguments must be in volts. `noise_volts` is the frame-to-frame
    standard deviation of a pixel, which is the only quantity here that is
    actually noise: it is the spread of repeated measurements of the same
    thing.

    `baseline_volts` is subtracted from the signal first. Signal-to-noise
    is a property of the measured quantity, not of the pedestal it sits
    on, so a fixed offset would otherwise inflate the result. Pass the
    dark level when one has been acquired; with dark subtraction already
    applied the baseline is zero and this argument can be omitted.

    Returns (ratio, decibels). Decibels use 20 log10 because these are
    amplitudes, not powers.
    """
    if not np.isfinite(noise_volts) or noise_volts <= 0:
        return np.nan, np.nan
    amplitude = abs(signal_volts - baseline_volts)
    if amplitude <= 0:
        return np.nan, np.nan
    ratio = amplitude / noise_volts
    return float(ratio), float(20.0 * np.log10(ratio))


def find_peaks(y, min_prominence, min_width=3, max_peaks=8):
    """Locate peaks by prominence, without scipy.

    Written directly rather than pulled from scipy so the application has
    only numpy and pyserial as dependencies, which keeps it straightforward
    to run on a machine that only needs to operate the instrument.

    Prominence is computed the standard way: for each local maximum, scan
    outward in both directions until the signal rises above the peak again,
    and take the peak height above the higher of the two intervening minima.
    Width is reported at half prominence.
    """
    y = np.asarray(y, dtype=np.float64)
    n = y.size
    filled = np.where(np.isfinite(y), y, -np.inf)

    candidates = []
    for i in range(1, n - 1):
        if filled[i] > filled[i - 1] and filled[i] >= filled[i + 1]:
            candidates.append(i)
    if not candidates:
        return []

    peaks = []
    for i in candidates:
        height = filled[i]

        j = i - 1
        left_min = height
        while j >= 0 and filled[j] < height:
            left_min = min(left_min, filled[j])
            j -= 1
        j = i + 1
        right_min = height
        while j < n and filled[j] < height:
            right_min = min(right_min, filled[j])
            j += 1

        base = max(left_min, right_min)
        prominence = height - base
        if not np.isfinite(prominence) or prominence < min_prominence:
            continue

        half = height - prominence / 2.0
        left = i
        while left > 0 and filled[left] > half:
            left -= 1
        right = i
        while right < n - 1 and filled[right] > half:
            right += 1
        width = float(right - left)
        if width < min_width:
            continue

        peaks.append({
            "index": int(i),
            "centre": (left + right) / 2.0,
            "height": float(height),
            "prominence": float(prominence),
            "width": width,
            "left": float(left),
            "right": float(right),
        })

    peaks.sort(key=lambda p: -p["prominence"])
    return peaks[:max_peaks]
