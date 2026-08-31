"""Wavelength calibration and session settings persistence.

Calibration maps pixel index to wavelength using a polynomial fitted to
reference points, each a pixel position with a known wavelength taken from
a source with well-defined emission lines.

Degree is chosen from the number of points rather than fixed. A polynomial
of degree n-1 through n points fits exactly and reports zero residual
regardless of how poor the underlying data is, which hides bad reference
points instead of exposing them. Capping the degree keeps the fit
overdetermined so the residual remains a real measure of quality.
"""

import json
import time
from pathlib import Path

import numpy as np

MAX_DEGREE = 3

# The device stores the reference points, not the fitted polynomial, as one
# line of text. These two must match CAL_TEXT_MAX and the command buffer in
# src/main.cpp: the whole request is "cal set " plus the text, and the
# firmware discards an over-long line rather than truncating it.
DEVICE_TEXT_MAX = 320
DEVICE_VERB_BYTES = 8

# The number of points that is guaranteed to fit. A point is at most 18
# characters ("1023.99:1099.9990,"), so 16 of them need 288 of the 319 usable.
DEVICE_POINTS_MAX = 16


class Calibration:
    """Pixel to wavelength mapping. Empty until at least two points are
    added, and reports itself as invalid until then so callers can fall
    back to pixel units rather than plotting a meaningless axis."""

    def __init__(self):
        self.points = []          # (pixel, wavelength_nm)
        self.coeffs = None        # numpy polynomial, highest order first
        self.degree = 0
        self.rms_nm = None

    @property
    def valid(self):
        return self.coeffs is not None

    def add_point(self, pixel, wavelength_nm):
        self.points = [p for p in self.points if abs(p[0] - pixel) > 0.5]
        self.points.append((float(pixel), float(wavelength_nm)))
        self.points.sort()
        self.refit()

    def remove_nearest(self, pixel, tolerance=12.0):
        if not self.points:
            return False
        idx = min(range(len(self.points)),
                  key=lambda i: abs(self.points[i][0] - pixel))
        if abs(self.points[idx][0] - pixel) > tolerance:
            return False
        del self.points[idx]
        self.refit()
        return True

    def clear(self):
        self.points = []
        self.coeffs = None
        self.degree = 0
        self.rms_nm = None

    def refit(self):
        if len(self.points) < 2:
            self.coeffs = None
            self.degree = 0
            self.rms_nm = None
            return
        px = np.array([p for p, _ in self.points], dtype=np.float64)
        wl = np.array([w for _, w in self.points], dtype=np.float64)
        # Degree n-1 through n points fits exactly and reports zero residual
        # whatever the data quality, so n-2 is used instead to leave at
        # least one degree of freedom and keep the RMS meaningful. Two
        # points are the exception: a line through two points is exact and
        # there is no lower degree available, so its zero residual should be
        # read as "not yet verifiable" rather than "perfect".
        self.degree = max(1, min(MAX_DEGREE, len(self.points) - 2))
        self.coeffs = np.polyfit(px, wl, self.degree)
        residual = wl - np.polyval(self.coeffs, px)
        self.rms_nm = float(np.sqrt(np.mean(residual ** 2)))

    def to_wavelength(self, pixels):
        if not self.valid:
            return None
        return np.polyval(self.coeffs, np.asarray(pixels, dtype=np.float64))

    def axis(self, n_pixels):
        """Wavelength for every pixel, or None if uncalibrated."""
        if not self.valid:
            return None
        return self.to_wavelength(np.arange(n_pixels))

    def residuals(self):
        if not self.valid:
            return []
        out = []
        for px, wl in self.points:
            fit = float(np.polyval(self.coeffs, px))
            out.append((px, wl, fit - wl))
        return out

    def describe(self):
        if not self.valid:
            return f"uncalibrated ({len(self.points)} point(s), need 2)"
        return (f"degree {self.degree}, {len(self.points)} points, "
                f"RMS {self.rms_nm:.3f} nm")

    def to_dict(self):
        return {
            "points": [list(p) for p in self.points],
            "coeffs": list(self.coeffs) if self.valid else None,
            "degree": self.degree,
            "rms_nm": self.rms_nm,
        }

    def from_dict(self, d):
        self.points = [tuple(p) for p in d.get("points", [])]
        self.refit()

    # ------------------------------------------------------ device store

    def to_device_text(self):
        """The points in the form the device stores.

        "<pixel>:<nm>,<pixel>:<nm>,..." and nothing else. The coefficients
        are deliberately not sent. They follow from the points and from the
        degree rule in refit(), and that rule lives here alone; a device
        holding both could hold two that disagree.

        Raises ValueError if the result would not fit the device command
        line, because a request that does not fit is discarded by the
        firmware rather than truncated, and would fail silently.
        """
        text = ",".join(f"{p:.2f}:{w:.4f}" for p, w in self.points)
        limit = DEVICE_TEXT_MAX - 1
        if len(text) > limit:
            raise ValueError(
                f"{len(self.points)} points need {len(text)} characters; "
                f"the device holds {limit}")
        return text

    def from_device_text(self, text):
        """Replace the points with what the device sent. Returns the count.

        Anything unparsable is skipped rather than raising. The device
        validates the character set but not the arithmetic, and a single bad
        pair should cost that pair, not the whole calibration.
        """
        points = []
        for part in str(text).split(","):
            part = part.strip()
            if not part or ":" not in part:
                continue
            px, _, wl = part.partition(":")
            try:
                points.append((float(px), float(wl)))
            except ValueError:
                continue
        self.points = sorted(points)
        self.refit()
        return len(self.points)

    def matches_device_text(self, text):
        """Whether the device already holds these points.

        Compared as the text that would be sent, so rounding to the stored
        precision cannot show up as a difference.
        """
        try:
            mine = self.to_device_text()
        except ValueError:
            return False
        return mine == str(text).strip()


class Settings:
    """Small JSON-backed store for things that should survive a restart:
    the calibration, the last port used, and acquisition parameters.

    Saving never raises. A settings file that cannot be written is an
    inconvenience, not a reason to interrupt an acquisition in progress.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.data = {}
        self.load()

    def load(self):
        try:
            if self.path.exists():
                self.data = json.loads(self.path.read_text())
        except Exception:
            self.data = {}
        return self.data

    def save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.data["saved"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self.path.write_text(json.dumps(self.data, indent=2))
            return True
        except Exception:
            return False

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value
