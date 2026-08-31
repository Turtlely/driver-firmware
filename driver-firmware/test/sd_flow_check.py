"""Drives the card browser through the real application.

Same stub as app_flow_check.py: the device is replaced at the serial write,
so the panel, the reader, the "D " line parsers and the record decoder are
all the code that ships.

The point of this check is the ROUTING. One record type arrives on one
stream from two different requests, "cap get" and "sd get", and the window
has to send each to the right place. A stored file that landed in the
capture state machine would look like a capture completing by itself; a
fresh capture that landed in the browser would silently discard a capture
the instrument had just spent a minute taking.
"""
import subprocess
import sys
import tkinter as tk
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "tools"))
from spectrometer.app import SpectrometerApp  # noqa: E402
from spectrometer.device import Device, parse_info  # noqa: E402

INFO = ("I fw=1.3.0 proto=3 board=rpipico2 pixels=1024 bits=14 vref_mv=2500 "
        "rate_hz=15 rate_min=1 rate_max=60 pixel_period_us=20 frame_us=66666 "
        "frame_us_min=20600 frame_us_max=10000000 refractory_us=5 "
        "sysclk_mhz=150 pio_crc=0x1 missing_code=0xFFFF stack=1 "
        "stack_block=16 stack_block_max=32 stack_sigma_x100=300 "
        "cap_max_frames=16384 cap_max_s=600 mode=0 sd=1 built=Aug_29_2026")

STATUS = ("D state=ok mounted=1 mhz=24 fat=16 size_mb=30436 free_mb=-1 "
          "next=4 files=3 writes=1 werr=0 auto=1 clock=1 codec=rle codec_n=1 "
          "last=SP000003.STK last_plain=12356 last_stored=7925 error=none\n")


def listing(entries, start, total):
    """The device's reply to one "sd list" request."""
    out = b""
    for name, tag, cid, dark, date in entries:
        out += (f"D file name={name} bytes=7925 plain=12356 codec=1 tag={tag} "
                f"id={cid} req=150 stack=148 exp_us=66666 dur_ms=10012 "
                f"dark={dark} valid=1 date={date}\n").encode()
    out += (f"D list_end start={start} shown={len(entries)} "
            f"total={total}\n").encode()
    return out


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
        self._buf.extend(data)
        while self._consume(self._buf):
            pass


assert parse_info(INFO).supports_storage, "sd=1 must set supports_storage"

root = tk.Tk()
root.withdraw()
# A settings file of its own. These checks drive the real window,
# and one that saved a calibration into the operator's store had
# it loaded by the next real session.
app = SpectrometerApp(root, settings_path=HERE / "build" /
                      "sd_flow_check_settings.json")
app.device = StubDevice()
dev = app.device

# ---------------------------------------------------------------- listing

app._sd_refresh()
# free=1, because the tab reports how full the card is and the device only
# measures that when it is asked.
assert dev.sent[-2] == "sd status free=1", dev.sent[-2]
assert dev.sent[-1] == "sd list start=0 count=24", dev.sent[-1]

# Two pages, because a card with more files than one request returns has to
# keep asking. The device reports the total at the end of every page and the
# window walks it from there.
dev.feed(STATUS.encode())
dev.feed(listing([("SP000001.STK", "SP", 1, 0, "2026-08-29T09:15:02")],
                 start=0, total=3))
app._poll_once()
assert dev.sent[-1] == "sd list start=1 count=24", dev.sent[-1]
assert app.sd_listing, "the walk must continue while pages remain"

dev.feed(listing([("DK000002.STK", "DK", 2, 1, "2026-08-29T09:20:44"),
                  ("SP000003.STK", "SP", 3, 0, "2026-08-29T09:31:10")],
                 start=1, total=3))
app._poll_once()
assert not app.sd_listing, "the walk must end when the total is reached"
assert len(app.sd_files) == 3, app.sd_files

rows = [app.sd_tree.item(i, "values") for i in app.sd_tree.get_children()]
assert [r[0] for r in rows] == ["SP000003.STK", "DK000002.STK",
                                "SP000001.STK"], rows
assert rows[1][2] == "dark", rows[1]
assert rows[0][4] == "66.67 ms", rows[0]
# The stored column reports what the codec saved, from the file's own fields.
assert rows[0][5] == "7.7 kB (1.56x)", rows[0]
# The card line says how it is formatted and how full it is, in GB, and
# nothing else. This STATUS carries free_mb=-1, so only the capacity is known.
assert app.sd_label.cget("text") == "FAT16, 29.7 GB", \
    app.sd_label.cget("text")
assert app.sd_auto_var.get() is True, "auto=1 must show as ticked"
assert app.sd_codec_var.get() == "rle", "the picker must follow the device"

# A page that returns nothing must end the walk even though the total says
# more exist, or the window asks for the same page for ever.
app.sd_files = []
app.sd_listing = True
app._on_sd_event("list_end", {"start": 0, "shown": 0, "total": 9})
assert not app.sd_listing, "an empty page must not loop"

# ------------------------------------------------------------ one file in

app._sd_refresh()
dev.feed(STATUS.encode())
dev.feed(listing([("SP000001.STK", "SP", 1, 0, "2026-08-29T09:15:02"),
                  ("DK000002.STK", "DK", 2, 1, "2026-08-29T09:20:44"),
                  ("SP000003.STK", "SP", 3, 0, "2026-08-29T09:31:10")],
                 start=0, total=3))
app._poll_once()

app.sd_tree.selection_set("SP000003.STK")
app._sd_view()
assert dev.sent[-1] == "sd get name=SP000003.STK", dev.sent[-1]
assert app.sd_fetch is not None and app.sd_fetch.name == "SP000003.STK"
assert app.stack_state is None, "reading a file is not a capture"

# The same record builder the firmware uses, compiled for this computer.
record = subprocess.run([str(HERE / "build" / "record_test.exe")],
                        capture_output=True, check=True).stdout

app.mask_ends.set(False)
rng = np.random.default_rng(11)
pos = 0
while pos < len(record):
    n = int(rng.integers(1, 700))
    dev.feed(record[pos:pos + n])
    pos += n
app._poll_once()

assert app.sd_fetch is None, "the fetch must end when the record arrives"
assert app.frozen is not None, "the stored spectrum was not displayed"
assert app.stack_display is not None, "the record must follow the display"
assert app.stack_state is None, "a stored file must not drive the capture"
# The window must NOT restart the live view for a file read. That belongs to
# a capture, which stops the frames itself; a browse never stopped them.
assert dev.sent[-1] == "sd get name=SP000003.STK", dev.sent[-1]

# Pixel p of the synthetic record holds code (p*7 % 16384).
expected = (500 * 7 % 16384) * 2.5 / (1 << 14)
assert abs(app.frozen[500] - expected) < 1e-5, (app.frozen[500], expected)
assert np.isnan(app.frozen[3]), "a pixel with no samples must stay missing"

# A file read must not have dark or flat applied. The references loaded now
# are not necessarily the ones that belong to a file taken last week.
app.dark = np.full(1024, 0.5)
app.use_dark.set(True)
volts = app._stack_to_volts(app.stack_display)
assert abs(volts[500] - expected) < 1e-5, "dark must not be applied to a file"
app.use_dark.set(False)

# ------------------------------------------------------------- the errors

app.sd_tree.selection_set("SP000001.STK")
app._sd_view()
assert app.sd_fetch is not None
dev.feed(b"E sd not_found\n")
saved = tk.messagebox.showwarning
tk.messagebox.showwarning = lambda *a, **k: None
try:
    app._poll_once()
finally:
    tk.messagebox.showwarning = saved
assert app.sd_fetch is None, "a refused request must end the wait"

# A capture that finishes while nothing is being browsed still reaches the
# capture state machine, which is the other half of the routing.
app.stack_state = "fetching"
app.stack_kind = "spectrum"
app.frozen = None
pos = 0
while pos < len(record):
    n = int(rng.integers(1, 700))
    dev.feed(record[pos:pos + n])
    pos += n
app._poll_once()
assert app.stack_state is None, "the capture path must have taken the record"
assert app.frozen is not None
assert dev.sent[-1] == "live 1", dev.sent[-1]

# A finished capture makes the listing stale, so the tab refreshes on show.
app.sd_dirty = False
app._on_sd_event("saved", "SP000004.STK")
assert app.sd_dirty, "a stored capture must mark the listing stale"

print("sd flow ok")
