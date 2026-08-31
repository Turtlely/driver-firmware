"""Drives one whole stacked capture through the real application.

The device is stubbed at the serial write, so everything above it is the code
that ships: the panel, the state machine, the reader, the record parser and
the display pipeline. The record itself comes from record_test.exe, which is
the firmware's own record builder compiled natively.

What this is checking is the wiring, not the arithmetic. The arithmetic is
checked by clip_compare.py and record_check.py.
"""
import subprocess
import sys
import tkinter as tk
from pathlib import Path

import numpy as np

# Paths from this file, not absolute: the checks have to run from a clone.
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "tools"))
from spectrometer import export, processing  # noqa: E402
from spectrometer.app import SpectrometerApp  # noqa: E402
from spectrometer.device import Device, parse_info  # noqa: E402

INFO = ("I fw=1.2.0 proto=3 board=rpipico2 pixels=1024 bits=14 vref_mv=2500 "
        "rate_hz=15 rate_min=1 rate_max=60 pixel_period_us=20 frame_us=66666 "
        "frame_us_min=20600 frame_us_max=10000000 refractory_us=5 "
        "sysclk_mhz=150 pio_crc=0x1 missing_code=0xFFFF stack=1 "
        "stack_block=16 stack_block_max=32 stack_sigma_x100=300 "
        "cap_max_frames=16384 cap_max_s=600 mode=0 built=Aug_29_2026")


class StubDevice(Device):
    """A Device whose port is a list. Everything above send() is real."""

    def __init__(self):
        super().__init__()
        self.sent = []
        self.info = parse_info(INFO)
        self.port = "STUB"
        self._buf = bytearray()

    @property
    def connected(self):
        return True

    def send(self, command):
        self.sent.append(command.strip())

    def feed(self, data):
        """Push bytes in as the reader thread would.

        One buffer for the life of the object, because that is what
        Device._run does and a record split across two reads only completes
        if the leftover survives."""
        self._buf.extend(data)
        while self._consume(self._buf):
            pass


root = tk.Tk()
root.withdraw()
# A settings file of its own. These checks drive the real window,
# and one that saved a calibration into the operator's store had
# it loaded by the next real session.
app = SpectrometerApp(root, settings_path=HERE / "build" /
                      "app_flow_check_settings.json")
app.device = StubDevice()
dev = app.device
app._update_device_label()

# The Preview integration field drives Capture without displaying redundant
# arithmetic beside the duration entry.
app.rate_mode.set("ms")
app.exposure_var.set("66.666")
app.stack_secs_var.set("10")
frames, us, note = app._stack_plan()
assert (frames, us, note) == (150, 66666, None), (frames, us, note)

# An invalid plan must be rejected before it reaches the device.
app.exposure_var.set("5")
_, _, note = app._stack_plan()
assert "below" in note, note
app.exposure_var.set("66.666")

app.stack_sigma_var.set("3.0")
app.stack_block_var.set("16")
app.stack_kind_var.set("spectrum")
app._start_stack()

assert app.stack_state == "running", app.stack_state
cmd = dev.sent[-1]
assert cmd == ("cap start secs=10.000 exp=66666 sigma=3.00 block=16 dark=0 "
               "tag=SP"), cmd
assert len(cmd) < 96, "command would not fit the firmware line buffer"
assert str(app.stack_start_btn.cget("state")) == "disabled"

# Progress. The device answers a status request with a C line.
dev.feed(b"C state=run id=7 req=150 att=57 stack=57 excl=0 exp_us=66666 "
         b"elapsed_ms=3800 result=0 mode=2 block=16 sigma_x100=300 miss=0 "
         b"stale=1 badbits=0 late_us=90 nostart=0\n")
app._poll_once()
assert app.stack_state == "running"
assert "57/150" in app.stack_label.cget("text"), app.stack_label.cget("text")
app._update_status()
assert "STACKING 57/150" in app.status.cget("text"), app.status.cget("text")

# Completion arrives unasked, and the app must then ask for the result.
dev.feed(b"C state=done id=7 req=150 att=150 stack=148 excl=2 exp_us=66666 "
         b"dur_ms=10012 result=1\n")
app._poll_once()
assert app.stack_state == "fetching", app.stack_state
assert dev.sent[-1] == "cap get", dev.sent[-1]

# The record, in ragged chunks, as the serial layer delivers it.
record = subprocess.run(
    [str(HERE / "build" / "record_test.exe")],
    capture_output=True, check=True).stdout
rng = np.random.default_rng(3)
pos = 0
while pos < len(record):
    n = int(rng.integers(1, 900))
    dev.feed(record[pos:pos + n])
    pos += n
app._poll_once()

assert app.stack_state is None, app.stack_state
assert str(app.stack_start_btn.cget("state")) == "normal"
assert app.frozen is not None, "the result was not displayed"
assert app.stack_display is not None
assert dev.sent[-1] == "live 1", dev.sent[-1]

# Pixel p of the synthetic record holds code (p*7 % 16384). Check the fixed
# notch and voltage conversion end to end.
app.mask_ends.set(False)
app.use_dark.set(False)
app.use_flat.set(False)
app.stack_kind = "spectrum"
app._finish_stack(app.stack_display)
codes = (np.arange(1024) * 7) % 16384
expected = processing.notch_pattern(codes * 2.5 / (1 << 14))[500]
assert abs(app.frozen[500] - expected) < 1e-5, (app.frozen[500], expected)
assert np.isnan(app.frozen[3]), "a pixel with no samples must stay missing"

# Saving records how it was produced.
out = HERE / "build" / "stack_save.txt"
export.write_spectrum(out, np.arange(1024), app.frozen, info=dev.info,
                      frames=app.stack_display.frames_stacked,
                      elapsed_s=app.stack_display.duration_ms / 1000.0,
                      stack=app.stack_display)
text = out.read_text(encoding="utf-8")

# Every header line is a field, "# key: value", apart from the title on the
# first line and the tab-separated peak rows. A sentence in a data file
# cannot be parsed and goes stale against the code that wrote it.
for n, line in enumerate(text.splitlines()):
    if not line.startswith("#"):
        break
    body = line[1:].strip()
    if n == 0 or not body or body.startswith(("peak\t", "columns")):
        continue
    assert ":" in body, f"header line is not a field: {line}"
for want in ("combined_on:        device", "stack_sigma:        3.00",
             "stack_stacked:      148", "device_stale:       9",
             "pixels_with_no_data: 1"):
    assert want in text, want

# A dark goes to the reference slot, not to the display.
result = app.stack_display
app.frozen = None
app.stack_display = None
app.stack_kind = "dark"
app._finish_stack(result)
assert app.dark is not None and app.use_dark.get()
assert app.frozen is None, "a dark must not take over the display"
assert app.stack_display is None

print("app flow ok")
