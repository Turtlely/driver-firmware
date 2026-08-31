"""Export spectra and reference arrays."""

import time

import numpy as np


def write_spectrum(path, pixels, volts, *, wavelengths=None, info=None,
                   frames=1, elapsed_s=0.0, dark_applied=False,
                   flat_applied=False, notch_applied=False,
                   calibration=None, peaks=None, note="", stack=None):
    # Header lines are "# key: value" and nothing else.
    #
    # They used to carry sentences explaining what the numbers meant. A data
    # file is the wrong place for that: the sentences cannot be parsed, they
    # go stale against the code that writes them, and they are already in
    # docs/APPLICATION.md. Every line here is a field.
    lines = []
    a = lines.append

    a("# S3903-1024Q spectrum")
    a(f"# saved:              {time.strftime('%Y-%m-%d %H:%M:%S')}")
    if note:
        a(f"# source:             {note}")
    if info is not None:
        a(f"# board:              {info.board}")
        a(f"# firmware:           {info.firmware}")
        a(f"# pixels:             {info.pixels}")
        a(f"# adc_bits:           {info.bits}")
        a(f"# vref_mv:            {info.vref_mv}")
        a(f"# frame_rate_hz:      {info.rate_hz}")
    a(f"# frames_combined:    {frames}")
    a(f"# integration_s:      {elapsed_s:.3f}")

    # A device stack was combined by firmware, with per-pixel outlier
    # rejection this file cannot show. Record how, and record what the frames
    # behind it looked like: nothing is transmitted during a capture, so these
    # counters are the only evidence the capture was sound, and a file without
    # them cannot be judged later.
    if stack is not None:
        a("# combined_on:        device")
        a(f"# stack_exposure_ms:  {stack.exposure_us / 1000.0:.3f}")
        a(f"# stack_duration_s:   {stack.duration_ms / 1000.0:.3f}")
        a(f"# stack_requested:    {stack.frames_requested}")
        a(f"# stack_attempted:    {stack.frames_attempted}")
        a(f"# stack_stacked:      {stack.frames_stacked}")
        a(f"# stack_excluded:     {stack.frames_excluded}")
        a(f"# stack_sigma:        {stack.sigma:.2f}")
        a(f"# stack_block_frames: {stack.block_frames}")
        a(f"# stack_clip_iters:   {stack.clip_iters}")
        if stack.rejected is not None:
            kept = int(stack.accepted.sum())
            cut = int(stack.rejected.sum())
            offered = kept + cut
            pct = (100.0 * cut / offered) if offered else 0.0
            a(f"# samples_kept:       {kept}")
            a(f"# samples_clipped:    {cut}")
            a(f"# samples_clipped_pct: {pct:.3f}")
            a(f"# pixels_with_no_data: "
              f"{int(np.count_nonzero(stack.accepted <= 0))}")
        a(f"# device_missing:     {stack.stat_missing}")
        a(f"# device_stale:       {stack.stat_stale}")
        a(f"# device_badbits:     {stack.stat_badbits}")
        a(f"# device_nostart:     {stack.stat_nostart}")
        a(f"# device_worst_late_us: {stack.stat_max_late_us}")
    else:
        a("# combined_on:        host")
    a(f"# dark_subtracted:    {'yes' if dark_applied else 'no'}")
    a(f"# flat_corrected:     {'yes' if flat_applied else 'no'}")
    a(f"# pattern_notched:    {'yes' if notch_applied else 'no'}")
    if notch_applied:
        a("# notch_khz:          17.4")

    valid_cal = calibration is not None and calibration.valid
    a(f"# calibrated:         {'yes' if valid_cal else 'no'}")
    if calibration is not None:
        a(f"# cal_point_count:    {len(calibration.points)}")
    if valid_cal:
        a(f"# cal_degree:         {calibration.degree}")
        a(f"# cal_rms_nm:         {calibration.rms_nm:.4f}")
        pts = ",".join(f"{p:.2f}:{w:.4f}" for p, w in calibration.points)
        a(f"# cal_points:         {pts}")

    valid = np.isfinite(volts)
    a(f"# pixels_total:       {len(volts)}")
    a(f"# pixels_valid:       {int(np.count_nonzero(valid))}")
    a("# missing_value:      NaN")

    if peaks:
        a(f"# peak_count:         {len(peaks)}")
        a("# columns_peak: n\tcentre_px\twavelength_nm\theight_v"
          "\tprominence_v\tfwhm_px")
        for i, pk in enumerate(peaks, 1):
            centre = pk["centre"]
            if wavelengths is not None:
                wl = f"{float(np.interp(centre, np.arange(len(wavelengths)), wavelengths)):.4f}"
            else:
                wl = "NaN"
            a(f"# peak\t{i}\t{centre:.2f}\t{wl}\t{pk['height']:.6f}"
              f"\t{pk['prominence']:.6f}\t{pk['width']:.1f}")

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
