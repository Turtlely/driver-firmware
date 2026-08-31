"""The histogram bar shading has to match the sigma bands it claims.

Three distributions, because the shading exists to tell them apart:
a Gaussian pixel, a pixel dominated by the 17.4 kHz interferer, and a
Gaussian pixel carrying the doubled samples an SPI clock slip produces.
The last two are the cases where a sigma clip behaves differently from
what someone reading a bare histogram would assume.
"""
import sys
import tkinter as tk
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "tools"))
from spectrometer import app as appmod  # noqa: E402
from spectrometer.app import (HIST_MARGIN_L, HIST_MARGIN_R,  # noqa: E402
                              HIST_W, SpectrometerApp)
from spectrometer.device import parse_info  # noqa: E402

root = tk.Tk()
root.withdraw()
# A settings file of its own. This check drives the real window, and the
# window applies the saved theme as it is built. Reading the operator's file
# made the result depend on which theme they last chose.
app = SpectrometerApp(root, settings_path=HERE / "build" /
                      "histogram_check_settings.json")
app.device.info = parse_info("I fw=1.2.0 proto=3 pixels=1024 bits=14 "
                             "vref_mv=2500 stack=1")

PX = 276
rng = np.random.default_rng(4)


def load(values_volts):
    app.pixel_history.clear()
    for v in values_volts:
        frame = np.full(1024, np.nan)
        frame[PX] = v
        app.pixel_history.append(frame)


def draw(tag):
    c = app.hist_canvas
    c.delete("all")
    c.configure(width=520, height=240)
    c.update_idletasks()
    app.hist_px = PX
    app._draw_pixel_histogram(PX, c)
    label = app.hist_label.cget("text")
    print(f"-- {tag}")
    for line in label.split("\n"):
        print("   " + line)
    return c, label


# 1. Gaussian: the bands should come out close to 68.3 / 27.2 / 4.3 / 0.3.
samples = rng.normal(1.2000, 0.0013, 500)          # volts, 1.3 mV sigma
load(samples)
c, label = draw("gaussian, sigma 1.3 mV")

mv = samples * 1000.0
mean, std = float(np.mean(mv)), float(np.std(mv, ddof=1))

# The same geometry the drawing code used, including its fallback for a
# canvas that has not been mapped yet.
_w = c.winfo_width()
if _w <= 1:
    _w = HIST_W
ml = HIST_MARGIN_L
plot_w = max(10, _w - HIST_MARGIN_L - HIST_MARGIN_R)
_, edges = np.histogram(mv, bins=int(np.clip(round(np.sqrt(500)), 8, 40)))
x_lo, x_hi = float(edges[0]), float(edges[-1])

# Every filled rectangle must carry the colour of the band its own centre
# falls in. This is the check that the bin splitting works: without it a wide
# bin straddling 1 sigma would paint colour on the wrong side of the line.
bad = 0
checked = 0
for item in c.find_all():
    if c.type(item) != "rectangle":
        continue
    fill = c.itemcget(item, "fill")
    # Read from the module, not from a name bound at import time: the
    # window rebinds the palette when it applies a theme, and a
    # snapshot taken before that matches nothing.
    bands = appmod.SIGMA_COLOURS + (appmod.SIGMA_OUTLIER,)
    if not fill or fill not in bands:
        continue
    x0, y0, x1, y1 = c.coords(item)
    checked += 1
    # Invert the x mapping the drawing code used.
    v_mid = x_lo + ((x0 + x1) / 2 - ml) / plot_w * (x_hi - x_lo)
    band = min(3, int(abs(v_mid - mean) / std))
    want = appmod.SIGMA_OUTLIER if band >= 3 else appmod.SIGMA_COLOURS[band]
    if fill != want:
        bad += 1
        if bad <= 3:
            print(f"   MISMATCH at {v_mid:.4f} mV "
                  f"({abs(v_mid-mean)/std:.2f} sigma): {fill} not {want}")
assert checked > 0, "no shaded bars were drawn"
assert bad == 0, f"{bad} of {checked} bars carry the wrong band colour"
print(f"   {checked} shaded segments, all in the right band")

got = [float(x) for x in label.split("\n")[1].replace("%", " ")
       .replace("(", " ").replace(")", " ").split()
       if x.replace(".", "").isdigit()]
assert 60 < got[0] < 76, got          # 1 sigma, near 68.3
assert 0 <= got[6] < 3, got           # >3 sigma, near 0.3

# 2. Arcsine: what a pixel dominated by the 17.4 kHz interferer looks like.
#    Hard edges near 1.4 sigma, so the 2 sigma band must come out empty.
phase = rng.uniform(0, 2 * np.pi, 500)
load(1.2 + 0.0047 * np.sin(phase))
_, label = draw("arcsine, as the 17.4 kHz interferer makes it")
got = [float(x) for x in label.split("\n")[1].replace("%", " ")
       .replace("(", " ").replace(")", " ").split()
       if x.replace(".", "").isdigit()]
assert got[4] == 0.0 and got[6] == 0.0, ("an arcsine pixel must show nothing "
                                         f"beyond 2 sigma, got {got}")
print("   nothing beyond 2 sigma, as an arcsine distribution requires")

# 3. Gaussian with injected doubled samples, the fault the clip exists for.
samples = rng.normal(0.6, 0.0013, 500)
samples[[10, 200, 401]] *= 2.0
load(samples)
_, label = draw("gaussian with three doubled samples")
assert "3 sample(s) a 3 sigma clip would reject" in label, label
print("   the three doubled samples are counted as clip fodder")

print("\nhistogram shading ok")
