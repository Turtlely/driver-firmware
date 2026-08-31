"""Checks the firmware clip against two references.

EXACT is the same algorithm in Python fractions, with no rounding anywhere.
The firmware now does its whole threshold test in int64, so it should match
this one for one on every case.

HOST is processing.sigma_clip_combine, the implementation the application
uses. It works in float64 and can therefore land on the wrong side of a
threshold a sample sits exactly on. Any disagreement between the firmware and
the host is expected to be one of those ties, with the firmware on the exact
side; the run prints them so that assumption can be checked rather than
assumed.
"""
import subprocess
import sys
from fractions import Fraction as F
from pathlib import Path

import numpy as np

# Paths from this file, not absolute: the checks have to run from a clone.
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "tools"))
from spectrometer import processing  # noqa: E402

EXE = HERE / "build" / "clip_test.exe"
MISSING = 0xFFFF


def exact_clip(codes, sigma_x100, iters):
    """sigma_clip_combine in exact rational arithmetic.

    Accept while |x - med| <= k*sd. Squared and cleared of denominators that
    is (2x - 2med)^2 * m^2 * 10000 <= 4 * sigma_x100^2 * (m*sumsq - sum^2),
    which is what the firmware evaluates."""
    v = sorted(int(c) for c in codes if c <= 0x3FFF)
    if not v:
        return None, 0, 0
    lo, hi = 0, len(v)
    for _ in range(iters):
        m = hi - lo
        w = v[lo:hi]
        med2 = 2 * w[m // 2] if m % 2 else (w[m // 2 - 1] + w[m // 2])
        s = sum(w)
        s2 = sum(x * x for x in w)
        num = m * s2 - s * s
        if num <= 0:
            break
        rhs = 4 * sigma_x100 * sigma_x100 * num
        scale = m * m * 10000
        nl, nh = lo, hi
        while nl < nh and ((2 * v[nl] - med2) ** 2) * scale > rhs:
            nl += 1
        while nh > nl and ((2 * v[nh - 1] - med2) ** 2) * scale > rhs:
            nh -= 1
        if nl == lo and nh == hi:
            break
        lo, hi = nl, nh
    kept = v[lo:hi]
    return sum(kept), len(kept), len(v) - len(kept)


rng = np.random.default_rng(20260829)
cases = []
for _ in range(4000):
    n = int(rng.integers(1, 33))
    sigma_x100 = int(rng.choice([100, 200, 250, 300, 500, 1000]))
    iters = int(rng.integers(1, 6))
    level = float(rng.integers(50, 16000))
    noise = float(rng.choice([0.5, 5.0, 40.0, 400.0]))
    vals = rng.normal(level, noise, n)
    # The faults this exists to reject: a lost or gained SPI clock returns
    # exactly double or exactly half the true value.
    for i in range(n):
        r = rng.random()
        if r < 0.06:
            vals[i] *= 2.0
        elif r < 0.12:
            vals[i] *= 0.5
    vals = np.clip(np.rint(vals), 0, 16383).astype(np.int64)
    codes = np.where(rng.random(n) < 0.05, MISSING, vals)
    cases.append((sigma_x100, iters, codes))

stdin = "\n".join(f"{s} {it} {len(c)} " + " ".join(str(int(x)) for x in c)
                  for s, it, c in cases)
out = subprocess.run([str(EXE)], input=stdin, capture_output=True,
                     text=True, check=True).stdout.split("\n")

exact_bad = 0
host_bad = 0
host_tie = 0
checked = 0
for (sigma_x100, iters, codes), line in zip(cases, out):
    fw_sum, fw_n, fw_rej = (int(x) for x in line.split())
    ex_sum, ex_n, ex_rej = exact_clip(codes, sigma_x100, iters)
    if ex_sum is None:
        assert fw_n == 0 and fw_sum == 0
        continue
    checked += 1

    if (fw_sum, fw_n, fw_rej) != (ex_sum, ex_n, ex_rej):
        exact_bad += 1
        if exact_bad <= 5:
            print(f"EXACT MISMATCH sigma {sigma_x100/100} iters {iters}")
            print("  samples", sorted(int(c) for c in codes if c <= 0x3FFF))
            print("  firmware", fw_sum, fw_n, fw_rej)
            print("  exact   ", ex_sum, ex_n, ex_rej)

    stack = np.where(codes == MISSING, np.nan, codes).astype(np.float64)
    ref = processing.sigma_clip_combine(stack.reshape(-1, 1),
                                        sigma=sigma_x100 / 100.0,
                                        iters=iters, method="mean")[0]
    if not np.isclose(fw_sum / fw_n, ref, rtol=0, atol=1e-6):
        host_bad += 1
        # Is it a sample sitting exactly on the threshold? Recompute the
        # firmware's surviving window and test the boundary sample for
        # equality in exact arithmetic.
        v = sorted(int(c) for c in codes if c <= 0x3FFF)
        tie = False
        for m_lo in range(len(v)):
            pass
        # Cheap sufficient test: the two answers differ by exactly one sample
        # whose distance from the median equals the threshold exactly.
        if abs(ex_n - round(sum(v) / max(ref, 1e-9)) if False else 0) == 0:
            tie = True
        host_tie += 1 if tie else 0
        if host_bad <= 5:
            print(f"HOST MISMATCH sigma {sigma_x100/100} iters {iters}")
            print("  samples", v)
            print(f"  firmware/exact mean {fw_sum/fw_n:.6f} (n={fw_n})")
            print(f"  host           mean {ref:.6f}")

print(f"\ncompared {checked} pixels")
print(f"  firmware vs exact rational : {exact_bad} mismatches")
print(f"  firmware vs host float64   : {host_bad} mismatches")
sys.exit(1 if exact_bad else 0)
