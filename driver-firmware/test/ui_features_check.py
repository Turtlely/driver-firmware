"""Drives the calibration store, the file browser and the view state.

Same stub as app_flow_check.py: the device is replaced at the serial write,
so the menu, the panels, the reader and the parsers are all the code that
ships.

What this covers:

  the calibration store   points out to the device and back in, and the
                          command line staying inside the firmware buffer
  the file browser        search, sort, and the rename request
  the view state          whether the button offers Play or Pause
  opening a file          a .STK read from this computer, with no device
"""
import subprocess
import sys
import tkinter as tk
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "tools"))
from spectrometer import stkfile  # noqa: E402
from spectrometer.app import SpectrometerApp  # noqa: E402
from spectrometer.calibration import (Calibration,  # noqa: E402
                                      DEVICE_POINTS_MAX, DEVICE_TEXT_MAX)
from spectrometer.device import Device, parse_info  # noqa: E402

INFO = ("I fw=1.4.0 proto=3 board=rpipico2 pixels=1024 bits=14 vref_mv=2500 "
        "rate_hz=15 rate_min=1 rate_max=60 pixel_period_us=20 frame_us=66666 "
        "frame_us_min=20600 frame_us_max=10000000 refractory_us=5 "
        "sysclk_mhz=150 pio_crc=0x1 missing_code=0xFFFF stack=1 "
        "stack_block=16 stack_block_max=32 stack_sigma_x100=300 "
        "cap_max_frames=16384 cap_max_s=600 mode=0 sd=1 cal=1 cal_n=0 "
        "cal_store=1 built=Aug_29_2026")

STATUS = ("D state=ok mounted=1 mhz=24 fat=32 size_mb=120580 vol_mb=120580 "
          "free_mb=118000 "
          "next=4 files=3 writes=1 werr=0 auto=1 clock=1 codec=rle codec_n=1 "
          "last=SP000003.STK last_plain=12356 last_stored=7925 error=none\n")


def listing(entries, start, total):
    out = b""
    for name, tag, cid, dark, date, exp in entries:
        out += (f"D file name={name} bytes=7925 plain=12356 codec=1 tag={tag} "
                f"id={cid} req=150 stack=148 exp_us={exp} dur_ms=10012 "
                f"dark={dark} valid=1 date={date}\n").encode()
    out += (f"D list_end start={start} shown={len(entries)} "
            f"total={total}\n").encode()
    return out


class StubDevice(Device):
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


root = tk.Tk()
root.withdraw()
# A settings file of its own. These checks drive the real window,
# and one that saved a calibration into the operator's store had
# it loaded by the next real session.
app = SpectrometerApp(root, settings_path=HERE / "build" /
                      "ui_features_check_settings.json")
app.device = StubDevice()
dev = app.device
app._update_device_label()

# ----------------------------------------------------------- the prefixes

# One prefix per kind of line, and each line under the kind that describes
# it. Two prefixes were carrying lines they did not describe: the per-frame
# trigger count arrived as an error, so the console showed it red and bold at
# the frame rate, and the hardware test results arrived as acknowledgements,
# so a line ending FAIL was drawn in the success colour.
dev.feed(b"T frame 7 filtered 1024 expected 1024 delta +0 "
         b"extrafirst 0us extragap 0-0us\n")
dev.feed(b"H selftest=PASS spi=PASS adcreset=PASS\n")
dev.feed(b"H sditest=FAIL failures=3\n")
dev.feed(b"E sd not_found\n")
dev.feed(b"A live=1\n")
tagged = [(app._log_tag_for(kind, text), text)
          for kind, text in dev.drain_messages()]
by_prefix = {text.split()[0]: tag for tag, text in tagged}
assert by_prefix["T"] == "trig", by_prefix
assert by_prefix["E"] == "error", by_prefix
assert by_prefix["A"] == "ack", by_prefix
# The verdict decides the colour, not the prefix: both of these are "H".
assert [t for t, x in tagged if "selftest" in x] == ["test"], tagged
assert [t for t, x in tagged if "sditest" in x] == ["testfail"], tagged

# The trigger count is one line per frame, so it is filtered with the frame
# statistics. That checkbox is off by default, which is what stops it
# flooding the console.
app.show_stats.set(False)
assert not app._console_filter_enabled("trig")
app.show_errors.set(True)
assert app._console_filter_enabled("error"), "an error must not be hidden"
app.show_stats.set(True)
assert app._console_filter_enabled("trig")
app.show_stats.set(False)

# --------------------------------------------------------------- the menu

# The one-shot actions live on the menu bar. Checked by label because that is
# the contract with the operator, and because a menu that fails to build
# raises here rather than on someone's bench.
menus = {}
last = app.menubar.index("end")
for i in range(last + 1):
    label = app.menubar.entrycget(i, "label")
    sub = app.root.nametowidget(app.menubar.entrycget(i, "menu"))
    menus[label] = [sub.entrycget(j, "label")
                    for j in range(sub.index("end") + 1)
                    if sub.type(j) != "separator"]
assert list(menus) == ["File", "Device", "Calibration", "View", "Help"], \
    list(menus)
assert "Open spectrum..." in menus["File"], menus["File"]
assert "Save to device" in menus["Calibration"], menus["Calibration"]
assert "Load from device" in menus["Calibration"], menus["Calibration"]
assert "Export to file..." in menus["Calibration"], menus["Calibration"]
# The theme lives here now, not beside the plot.
assert "Dark mode" in menus["View"], menus["View"]
# The device identifies itself when the port opens, so there is nothing for
# an Identify item to do.
assert not any("Identify" in x for x in menus["Device"]), menus["Device"]
# The Connect item says what pressing it will do.
assert "Disconnect" in menus["Device"], menus["Device"]

# Switching the theme repaints every widget that was given a colour when it
# was built. Removing one of those from the window without removing it from
# _set_theme leaves a switch that raises, and nothing else would catch it.
app.dark_var.set(True)
app._toggle_theme()
assert app.settings.get("theme") == "dark", app.settings.get("theme")
app.dark_var.set(False)
app._toggle_theme()
assert app.settings.get("theme") == "light", app.settings.get("theme")

# ------------------------------------------------------ calibration store

assert dev.info.supports_calibration, "cal=1 must set supports_calibration"

# The device stores the reference POINTS. Coefficients follow from them, and
# the degree rule lives only in calibration.py, so a device that also held
# coefficients could hold two answers that disagree.
app.cal.clear()
for px, nm in ((12.0, 435.833), (300.5, 546.074), (612.25, 611.492),
               (880.2, 696.543)):
    app.cal.add_point(px, nm)
text = app.cal.to_device_text()
assert text == ("12.00:435.8330,300.50:546.0740,612.25:611.4920,"
                "880.20:696.5430"), text

app._cal_push_to_device()
assert dev.sent[-1] == f"cal set {text}", dev.sent[-1]
# The firmware discards an over-long line instead of truncating it, so a
# request that does not fit fails silently. See pollHostCommands().
assert len(dev.sent[-1]) < 384, "the request must fit the firmware buffer"

# Sixteen points is the documented capacity. It has to fit even in the worst
# case, which is a four digit pixel and a four digit wavelength.
big = Calibration()
for i in range(DEVICE_POINTS_MAX):
    big.add_point(1023.99 - i, 1099.999 - i)
assert len(big.to_device_text()) < DEVICE_TEXT_MAX, len(big.to_device_text())
assert len(f"cal set {big.to_device_text()}") < 384, "must fit buf[384]"

# Past that it must be refused here rather than vanishing on the wire: the
# firmware discards an over-long line instead of truncating it.
while len(big.points) < 40:
    big.add_point(2000.0 + len(big.points), 900.0)
try:
    big.to_device_text()
except ValueError as exc:
    assert "device holds" in str(exc), str(exc)
else:
    raise AssertionError("an over-long point list must not be sent")

# The reply comes back on its own letter, and the round trip must land on
# the same points.
app.cal.clear()
app._cal_pull_from_device()
assert dev.sent[-1] == "cal get", dev.sent[-1]
dev.feed(f"K cal n=4 bytes={len(text)} store=1 points={text}\n".encode())
app._poll_once()
assert len(app.cal.points) == 4, app.cal.points
assert app.cal.valid, "four points must give a usable fit"
assert app.cal.matches_device_text(text), app.cal.to_device_text()
assert app.cal_pull_pending is None, "the wait must end with the reply"
assert dev.info.cal_n == 4 and dev.info.cal_store == 1

# A device with nowhere to keep it still answers, and the window says so
# rather than pretending the calibration is safe.
dev.feed(b"K cal n=0 bytes=0 store=0 points=none\n")
app._poll_once()
assert dev.info.cal_store == 0
assert "unavailable" in app.cal_device_label.cget("text"), \
    app.cal_device_label.cget("text")

# ------------------------------------------------------------ the browser

app._sd_refresh()
dev.feed(STATUS.encode())
dev.feed(listing([("SP000001.STK", "SP", 1, 0, "2026-08-29T09:15:02", 66666),
                  ("DK000002.STK", "DK", 2, 1, "2026-08-29T09:20:44", 20000),
                  ("LASER1.STK", "SP", 3, 0, "2026-08-29T09:31:10", 40000)],
                 start=0, total=3))
app._poll_once()

names = [app.sd_tree.item(i, "values")[0] for i in app.sd_tree.get_children()]
# A renamed file carries no sequence number, so it sorts as 0 and lands last
# under the default order. Nothing depends on the number: the capture id in
# the record is the identity.
assert names == ["DK000002.STK", "SP000001.STK", "LASER1.STK"], names

# How full the card is, in GB, and how it is formatted. Nothing else.
#
# Measured against the mounted VOLUME, not the card. This STATUS has the two
# the same, so the difference does not show here; the cases below cover it.
assert app.sd_label.cget("text") == "FAT32, 2.52 of 117.8 GB used (2.1 %)", \
    app.sd_label.cget("text")
assert app.sd_count_label.cget("text") == "3 files", \
    app.sd_count_label.cget("text")

# --------------------------------------------------- card size and volume

# The card and the mounted volume are separate numbers. Files live on the
# volume, so the volume is what fills up. Reported by a 128 GB card carrying
# a small FAT16 partition, which is a formatting fault worth naming: most of
# the card is unreachable and nothing else would say so.
from spectrometer.device import parse_sd_status  # noqa: E402

SD = ("D state=ok mounted=1 mhz=24 fat={fat} size_mb={size} vol_mb={vol} "
      "free_mb={free} next=4 files=3 writes=1 werr=0 auto=1 clock=1 "
      "codec=rle codec_n=1 last=x last_plain=1 last_stored=1 error=none")

short = parse_sd_status(SD.format(fat=16, size=120580, vol=3800, free=3000))
assert short.partition_is_short, short.summary()
assert short.capacity_mb == 3800, short.capacity_mb
assert "of 3.7 GB used" in short.summary(), short.summary()
assert "card 117.8 GB" in short.summary(), short.summary()

# A 1 TB card. exFAT is what one ships formatted as, and fat=64 is how the
# device reports exFAT; spelling it "FAT64" would name a filesystem that
# does not exist.
tb = parse_sd_status(SD.format(fat=64, size=976562, vol=976000, free=900000))
assert not tb.partition_is_short, tb.summary()
assert tb.summary().startswith("exFAT, 74.22 of 953.1 GB used"), tb.summary()

# Free space the device could not measure is unknown, not zero. SdFat widens
# the volume's -1 error into 0xFFFFFFFF, so the firmware screens it and sends
# -1; nothing here may report that as a full or an empty card.
unknown = parse_sd_status(SD.format(fat=32, size=30436, vol=30400, free=-1))
assert unknown.used_gb is None and unknown.used_fraction is None
assert unknown.summary() == "FAT32, 29.7 GB", unknown.summary()

# Firmware from before vol_mb existed sends only the card size, and must
# still produce a sensible line rather than dividing by zero.
old = parse_sd_status(SD.format(fat=32, size=30436, vol=0, free=15000)
                      .replace("vol_mb=0 ", ""))
assert old.capacity_mb == 30436, old.capacity_mb
assert not old.partition_is_short
assert "of 29.7 GB used" in old.summary(), old.summary()

# Search filters what is already in hand. It sends nothing to the device, so
# it stays instant while a capture runs.
before = len(dev.sent)
app.sd_search_var.set("laser")
app._sd_fill_tree()
names = [app.sd_tree.item(i, "values")[0] for i in app.sd_tree.get_children()]
assert names == ["LASER1.STK"], names
assert app.sd_count_label.cget("text") == "1 of 3 files"
assert len(dev.sent) == before, "search must not talk to the device"

# The kind is searchable as well as the name.
app.sd_search_var.set("dark")
app._sd_fill_tree()
names = [app.sd_tree.item(i, "values")[0] for i in app.sd_tree.get_children()]
assert names == ["DK000002.STK"], names
app._sd_clear_search()
assert app.sd_count_label.cget("text") == "3 files"

# Clicking a heading sorts by it; clicking the same one again reverses.
app._sd_sort_by("name")
names = [app.sd_tree.item(i, "values")[0] for i in app.sd_tree.get_children()]
assert names == ["DK000002.STK", "LASER1.STK", "SP000001.STK"], names
app._sd_sort_by("name")
names = [app.sd_tree.item(i, "values")[0] for i in app.sd_tree.get_children()]
assert names == ["SP000001.STK", "LASER1.STK", "DK000002.STK"], names
assert app.sd_tree.heading("name", "text").startswith("File"), \
    app.sd_tree.heading("name", "text")

app._sd_sort_by("exposure")
names = [app.sd_tree.item(i, "values")[0] for i in app.sd_tree.get_children()]
assert names == ["SP000001.STK", "LASER1.STK", "DK000002.STK"], names

# Rename. The dialog is replaced, because a check must not wait for a click.
import tkinter.simpledialog as simpledialog  # noqa: E402

app.sd_tree.selection_set("SP000001.STK")
saved_ask = simpledialog.askstring
simpledialog.askstring = lambda *a, **k: "  hg_lamp "
try:
    app._sd_rename()
finally:
    simpledialog.askstring = saved_ask
assert dev.sent[-1] == "sd rename name=SP000001.STK to=HG_LAMP.STK", \
    dev.sent[-1]

# A name the card already holds is refused here, before the request is made.
saved_warn = tk.messagebox.showwarning
tk.messagebox.showwarning = lambda *a, **k: None
before = len(dev.sent)
simpledialog.askstring = lambda *a, **k: "LASER1"
try:
    app._sd_rename()
    # And so is a name that FAT cannot hold.
    simpledialog.askstring = lambda *a, **k: "way too long a name"
    app._sd_rename()
finally:
    simpledialog.askstring = saved_ask
    tk.messagebox.showwarning = saved_warn
assert len(dev.sent) == before, "a bad rename must not reach the device"

# The device confirming a rename refreshes the listing.
app.sd_dirty = False
dev.feed(b"D renamed name=SP000001.STK to=HG_LAMP.STK\n")
app._poll_once()
assert app.sd_dirty or dev.sent[-1].startswith("sd list"), dev.sent[-1]

# ---------------------------------------------------------- the view state

# The button says what pressing it will do. Loading a stored spectrum leaves
# the device transmitting, so a label taken from live_on alone said "Pause"
# over a frozen trace that nothing was updating.
app.frozen = None
app.stack_display = None
app.live_on = True
app._update_live_btn()
assert app.live_btn.cget("text") == "Pause", app.live_btn.cget("text")

# The record comes from the firmware's own builder, compiled for this
# computer. Without g++ it is absent, and the two sections that need a real
# record are skipped rather than failing: everything above this line, which
# is most of what this check covers, needs no compiler.
record_exe = HERE / "build" / ("record_test.exe" if sys.platform == "win32"
                               else "record_test")
card = HERE / "build" / "card_peaks.STK"
have_record = record_exe.exists() and card.exists()
if not have_record:
    print("    record_test / card_peaks.STK absent: skipping the file "
          "sections")

if have_record:
    record = subprocess.run([str(record_exe)],
                            capture_output=True, check=True).stdout
    app.sd_tree.selection_set("LASER1.STK")
    app._sd_view()
    dev.feed(record)
    app._poll_once()
    assert app.frozen is not None, "the stored spectrum was not displayed"
    assert app.live_btn.cget("text") == "Play", app.live_btn.cget("text")
else:
    app.frozen = np.zeros(1024)
    app._update_live_btn()
    assert app.live_btn.cget("text") == "Play", app.live_btn.cget("text")

# Pressing it puts the held spectrum away. The device is already
# transmitting, so nothing needs to be asked of it.
before = len(dev.sent)
app._toggle_live()
assert app.frozen is None and app.stack_display is None
assert app.live_btn.cget("text") == "Pause", app.live_btn.cget("text")
assert len(dev.sent) == before, "resuming a live view needs no command"

# Pausing does stop the device.
app._toggle_live()
assert dev.sent[-1] == "live 0", dev.sent[-1]
assert app.live_btn.cget("text") == "Play", app.live_btn.cget("text")
app.live_on = True

# --------------------------------------------------- opening from the disk

# The same decoder the standalone reader uses, so a card in a card reader
# opens exactly like a stored file does. No device is involved.
if have_record:
    result, codec, stored = stkfile.load(card)
    assert result.pixels == 1024, result.pixels
    assert codec in ("none", "rle"), codec
    assert stored > 0

    app.frozen = None
    app.stack_display = None
    saved_open = tk.filedialog.askopenfilename
    tk.filedialog.askopenfilename = lambda *a, **k: str(card)
    try:
        app._open_spectrum_file()
    finally:
        tk.filedialog.askopenfilename = saved_open
    assert app.frozen is not None, "the file was not displayed"
    assert app.stack_display is not None, "the record must follow the display"
    assert app.live_btn.cget("text") == "Play", app.live_btn.cget("text")

# A text file this program wrote reads back as the same volts.
out = HERE / "build" / "ui_roundtrip.txt"
volts = np.linspace(0.1, 1.9, 1024)
volts[7] = np.nan
from spectrometer import export  # noqa: E402

export.write_spectrum(out, np.arange(1024), volts, info=dev.info)
back = app._read_text_spectrum(out)
assert back.size == 1024, back.size
assert np.isnan(back[7]), "a missing pixel must not read back as zero"
assert abs(back[500] - volts[500]) < 1e-6, (back[500], volts[500])

print("ui features ok")
