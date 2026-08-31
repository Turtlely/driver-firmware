"""Tk application for the S3903-1024Q spectrometer."""

import json
import sys
import time
import tkinter as tk
from collections import deque
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, simpledialog, ttk

import numpy as np

from . import export, processing, resources, stkfile, typography
from .calibration import Calibration, Settings
from .device import (CODEC_NAMES, MAX_CODE, Device, DeviceError,
                     find_devices)

APP_NAME = "Helios"
AUTHOR = "Made by Ryan Tang"
POLL_MS = 40

# The console and the numeric readouts. Named here rather than repeated as a
# string so the split between it and the interface face stays visible: see
# typography.py for why the two are not the same font.
MONO = typography.MONO_FAMILY

# Filled in once the Tk root exists, because choosing a family means asking
# the system which ones it has. Until then the interface has no font to name.
UI = "Segoe UI"
UI_SEMI = "Segoe UI"

# The five stripes of the mark, top to bottom. Fixed, not themed: a logo
# that changes colour with the window is not a logo.
LOGO_STRIPES = ("#0099ff", "#7cde00", "#fefe00", "#ff8000", "#ff0000")

# How often a running on-device capture is asked for progress, in poll ticks.
#
# The device does not report progress by itself, and that is deliberate: it
# transmits nothing at all while stacking, so the USB interrupts that cost
# pixels during a scan stay out of the capture. Completion does arrive
# unasked, so this only drives the progress display and can be slow.
STACK_POLL_TICKS = 13   # about twice a second at POLL_MS = 40

# How long to wait for a result record before asking for it again, and how
# many times to ask.
#
# The record is 12 KB and arrives in well under a second, so anything near
# this limit means the transfer was lost rather than slow. Asking again is
# worth doing because the device keeps the result until the next capture
# overwrites it: a lost transfer is a retry, not a lost capture. Bounded so a
# device that has stopped answering ends as a message rather than as a window
# stuck on "collecting result".
STACK_FETCH_TIMEOUT_S = 8.0
STACK_FETCH_TRIES = 3

# Files asked for in one directory request. The device caps this at 24: its
# transmit queue holds about 3 KB and a listing line is roughly 130 bytes, so
# a whole card in one reply would overrun the queue and lose lines. A card
# with more files than this is read a page at a time, driven by the total the
# device reports at the end of each page.
SD_PAGE = 24

# How long to wait for a stored file before giving up on it. Shorter than
# STACK_FETCH_TIMEOUT_S: this transfer has no capture behind it, so a retry
# costs nothing but the wait.
SD_FETCH_TIMEOUT_S = 8.0

# Extra time allowed on top of a capture's own predicted length before it is
# treated as lost. Generous: a capture that runs long is a capture whose
# frames came late, which is a real thing that happens, and ending it early
# would throw away good data.
STACK_RUN_GRACE_S = 30.0

PLOT_W = 1024
PLOT_H = 320
STRIP_H = 44
MARGIN_L = 62
MARGIN_R = 12
MARGIN_T = 20
MARGIN_B = 36

PIXEL_HISTORY_LEN = 500
HIST_W = 380
HIST_H = 200
HIST_MARGIN_L = 50
HIST_MARGIN_R = 26
HIST_MARGIN_T = 14
HIST_MARGIN_B = 34

SPACE_SPEC_FRAMES = 60
SPACE_SPEC_PEAK_MIN_HZ = 15000.0

# Two palettes. Every colour the app draws with is named here, and the
# drawing code reads these names as module globals when it runs. So
# _apply_palette() rebinds the globals and a redraw repaints everything.
#
# Widgets that were given a colour when they were built do not re-read a
# global. Those are handled in SpectrometerApp._set_theme.
THEMES = {
    "light": {
        "BG": "#ffffff",
        "PANEL": "#f4f6f8",
        "FG": "#111827",
        "FG_DIM": "#46515f",
        "GRID": "#d7dce2",
        "GRID_MAJOR": "#aeb7c2",
        "AXIS": "#46515f",
        "TRACE": "#075aa6",
        "TRACE_REF": "#6f42c1",
        "MISSING": "#b42318",
        "PEAK": "#9a4d00",
        "CURSOR": "#687386",
        "STRIP_BORDER": "#aeb7c2",
        "OK": "#116329",
        "CONTROL": "#e4e8ed",
        "CONTROL_HOVER": "#d7dde4",
        "CONTROL_PRESSED": "#c8d0d9",
        "INPUT": "#ffffff",
        "BORDER": "#c8cfd7",
        "BORDER_SOFT": "#e7ebef",
        "SELECT": "#075aa6",
        "DISABLED_FG": "#707b87",
        "SIGMA_COLOURS": ("#075aa6", "#4f83b5", "#91b2d1"),
    },
    "dark": {
        # Neutral, slightly warm charcoal. The contrast steps are deliberately
        # close together: this is an instrument UI, not a neon dashboard.
        "BG": "#1e2227",
        "PANEL": "#272c33",
        "FG": "#e5e9ef",
        "FG_DIM": "#a3adb9",
        "GRID": "#30363e",
        "GRID_MAJOR": "#444c57",
        "AXIS": "#9da7b3",
        "TRACE": "#67b0f5",
        "TRACE_REF": "#bd93e8",
        "MISSING": "#f47067",
        "PEAK": "#e9a35a",
        "CURSOR": "#7f8996",
        "STRIP_BORDER": "#444c57",
        "OK": "#57c47a",
        "CONTROL": "#30363e",
        "CONTROL_HOVER": "#3a424c",
        "CONTROL_PRESSED": "#252a30",
        "INPUT": "#252a30",
        "BORDER": "#414954",
        "BORDER_SOFT": "#31373f",
        "SELECT": "#356fa8",
        "DISABLED_FG": "#737d89",
        "SIGMA_COLOURS": ("#67b0f5", "#477faf", "#345a78"),
    },
}

THEME = "light"

BG = PANEL = FG = FG_DIM = GRID = GRID_MAJOR = AXIS = ""
TRACE = TRACE_REF = MISSING = PEAK = CURSOR = STRIP_BORDER = OK = ""
CONTROL = CONTROL_HOVER = CONTROL_PRESSED = INPUT = BORDER = SELECT = ""
BORDER_SOFT = ""
DISABLED_FG = ""
SIGMA_COLOURS = ()
SIGMA_OUTLIER = ""
LOG_STYLES = {}


def _apply_palette(name):
    """Rebind the colour globals, and the tables built from them."""
    global THEME, SIGMA_OUTLIER, LOG_STYLES
    if name not in THEMES:
        name = "light"
    THEME = name
    globals().update(THEMES[name])

    # Built from the palette, so they are rebuilt with it.
    SIGMA_OUTLIER = MISSING
    LOG_STYLES = {
        "sent":  {"foreground": TRACE, "font": (MONO, 8, "bold")},
        "ack":   {"foreground": OK},
        "error": {"foreground": MISSING, "font": (MONO, 8, "bold")},
        "info":  {"foreground": TRACE_REF},
        "stat":  {"foreground": FG_DIM},
        # One line per frame, so it is dimmed like the frame statistics it
        # belongs with rather than shouted like the errors it used to share
        # a prefix with.
        "trig":  {"foreground": FG_DIM},
        # A hardware test result. Coloured by its own verdict in
        # _log_tag_for(), so the two entries here are the styles it picks
        # between, not a single fixed colour.
        "test":  {"foreground": OK, "font": (MONO, 8, "bold")},
        "testfail": {"foreground": MISSING, "font": (MONO, 8, "bold")},
        "cap":   {"foreground": OK, "font": (MONO, 8, "bold")},
        "local": {"foreground": FG_DIM, "font": (MONO, 8, "italic")},
        "text":  {"foreground": FG},
    }


_apply_palette("light")

# Histogram bar shading, by distance from the mean in standard deviations.
#
# Not decoration. The outermost band is exactly what a stacked capture at 3
# sigma throws away, so this shows what the clip is acting on before a capture
# is run, and the printed fractions say whether the distribution is Gaussian
# enough for that clip to mean anything. A pixel dominated by the 17.4 kHz
# interferer is arcsine distributed, not Gaussian: it has hard edges near
# 1.4 sigma and nothing at all beyond them, which shows here as an empty 2
# sigma band and a visibly flat-topped or two-humped shape.
# Fraction of a normal distribution inside each band: within 1 sigma, between
# 1 and 2, between 2 and 3, and beyond 3.
SIGMA_EXPECTED = (0.682689, 0.271810, 0.042800, 0.002700)


# Commands the firmware accepts, taken from handleCommand() and
# handleDiagnosticCommand() in src/main.cpp. Used for the console's help
# text and for Tab completion. Keep in step with the firmware; an entry
# missing here only costs completion, since anything typed is sent
# through and the firmware answers "E unknown_command".
DEVICE_COMMANDS = [
    ("id", "", "Report firmware version and current settings."),
    ("ver", "", "Same as id."),
    ("rate", "<hz>", "Set the frame rate in hertz. 0 runs free."),
    ("period", "<us>", "Set the frame period in microseconds."),
    ("verbose", "<0-2>", "Set how much text accompanies each frame."),
    ("cfr", "", "Read the ADC configuration register 8 times."),
    ("cfrw", "<hex>", "Write a configuration register value and read it back."),
    ("sclk", "<nops>", "Set the SCLK half period, 0 to 1000. 0 is normal."),
    ("readdelay", "<us>", "Set a settling delay before each read, 0 to 10."),
    ("selftest", "", "Run the SPI self check and the ADC reset, the same "
                     "sequence the device runs at boot."),
    ("adcreset", "", "Reset the ADC and verify the reset took effect."),
    ("sditest", "", "Test the SDI line end to end."),
    ("cfrloop", "<secs>", "Repeat the read-CFR transfer for 1 to 10 seconds."),
    ("pindiag", "", "Test the four ADC pins for a stuck net."),
    ("live", "<0|1>", "Start or stop frame transmission."),
    ("cap start",
     "secs=<s> exp=<us> [sigma=<f>] [block=<n>] [dark=<0|1>] [tag=<TT>]",
     "Run a stacked capture on the device. tag is the two letter file name "
     "prefix."),
    ("cap stop", "", "End a running capture and keep what it collected."),
    ("cap status", "", "Report progress of the current or last capture."),
    ("cap get", "", "Send the stored stack result."),
    ("sd status", "[free=1]", "Report the card. free=1 also measures free "
                              "space."),
    ("sd mount", "", "Attach a card inserted after boot."),
    ("sd unmount", "", "Release the card."),
    ("sd auto", "<0|1>", "Write every finished capture to the card, or stop."),
    ("sd codec", "[none|rle]", "Set or report how new files are written."),
    ("sd time", "<epoch>", "Set the clock used to date files. Local seconds."),
    ("sd list", "[start=<n>] [count=<n>]", "List stored spectra, one page."),
    ("sd get", "name=<file>", "Send one stored file back."),
    ("sd save", "[tag=<TT>]", "Write the result in device RAM to the card."),
    ("sd del", "name=<file>", "Delete one file from the card."),
    ("sd rename", "name=<file> to=<file>",
     "Rename one file. Refused if the new name is taken."),
    ("cal get", "", "Report the calibration points held in device flash."),
    ("cal set", "<px>:<nm>,<px>:<nm>,...",
     "Replace the stored calibration points and write them to flash."),
    ("cal clear", "", "Erase the stored calibration."),
]

# Typed here, these change device state the Acquisition panel also owns.
# Re-identify after them so the panel does not keep showing a stale rate.
RESYNC_COMMANDS = ("rate", "period", "live")

CONSOLE_HISTORY_LEN = 200


DIAGNOSTIC_LEGEND = [
    ("pulses", "1024", "Pixels captured this frame."),
    ("raw", "= pulses", "Triggers counted without debounce."),
    ("miss", "0", "Pixel slots with no value."),
    ("period", "20 us", "Median interval between pixel triggers."),
    ("wdog", "0", "Scans that did not deliver every pixel in time."),
    ("nostart", "0", "START pulses the CPLD ignored after all retries."),
    ("retry", "0", "Extra START pulses issued."),
    ("early", "= pulses",
     "Reads where the trigger line released before the transfer ended."),
    ("fifo", "1", "Deepest trigger backlog seen, out of 4."),
    ("gap", "20 to 20 us",
     "Shortest and longest interval between accepted triggers."),
    ("late", "under ~1000 us", "How late the frame was against its schedule."),
    ("dur", "~20500 us", "Wall clock time for the whole frame."),
    ("maxread", "3 to 4 us", "Longest single ADC read this frame."),
    ("txdrop", "not rising",
     "Frames discarded because the host was still reading the previous one."),
    ("lowbits", "all in first bucket",
     "Distribution of the two unused bits of each converter word."),
    ("stale", "0",
     "Triggers discarded because another had already queued behind them."),
    ("px<n>", "-", "Value of one pixel, as a code and in millivolts."),
]


class SpectrometerApp:
    def __init__(self, root, settings_path=None):
        """`settings_path` overrides where the session store lives.

        It exists for the checks in test/. They drive the real window, and a
        calibration added by a check was being written into the operator's
        own settings file and then loaded by the next real session.
        """
        self.root = root
        self.device = Device()
        self.settings = Settings(
            settings_path
            or (Path.home() / ".s3903_spectrometer" / "settings.json"))
        self.cal = Calibration()
        self.cal.from_dict(self.settings.get("calibration", {}))
        # None, "auto" or "menu": which request a "K cal" reply is answering.
        # See _on_cal_event.
        self.cal_pull_pending = None

        # The chosen serial port, as a bare device name. Built here rather
        # than in the menu, because the menu is rebuilt whenever it is opened
        # and the choice has to outlive that. An older settings file holds
        # the combined "COM4  description" form; _selected_port() takes the
        # first field, so both read correctly.
        self.port_var = tk.StringVar(
            value=str(self.settings.get("port", "") or ""))
        self._ports = []

        self.pixels = self.device.info.pixels
        self.accum = processing.Accumulator(self.pixels, 1)
        self.dark = None
        self.flat = None
        self.live_volts = None
        # Whether the DEVICE is transmitting frames. The firmware boots into
        # LIVE, and _on_info corrects this from the mode the device reports if
        # a previous session left it quiet.
        self.live_on = True
        self.display = None
        self.frozen = None

        # Per-frame values (not the running mean) for every pixel, kept so
        # a clicked pixel's distribution and spectra can be shown. This is
        # the corrected pipeline's output (dark/flat/notch/mask applied as
        # those toggles are set), i.e. the same values the plot displays;
        # "raw" in the sense used elsewhere in this file means "not
        # Accumulator-averaged," not "uncorrected." See PIXEL_HISTORY_LEN.
        self.pixel_history = deque(maxlen=PIXEL_HISTORY_LEN)
        # A second, genuinely uncorrected buffer (codes_to_volts only, no
        # dark/flat/notch/mask) used exclusively by the space-domain
        # spectrum. That view's entire purpose is showing whatever the
        # 17.4 kHz notch (processing.notch_pattern, on by default) would
        # otherwise remove; sourcing it from the corrected buffer above
        # would make it blind to the one thing it exists to reveal.
        # Confirmed live: with the notch on, a spatial spectrum built from
        # self.pixel_history showed no trace of the interferer at all.
        self.pixel_history_raw = deque(maxlen=PIXEL_HISTORY_LEN)
        self.hist_px = None
        # Sample rate the time-axis spectrum's frequency scale is locked
        # to. Captured once when history starts accumulating rather than
        # re-read from the live, slightly-fluctuating fps every redraw,
        # which was visibly rescaling the axis on every frame. Reset to
        # None (recapture on next draw) whenever the history is cleared.
        self.hist_fs = None

        self.capture_target = 0
        self.capture_frames = []
        self.capture_kind = None
        self.capture_started = 0.0

        # On-device stacked capture. None, "running" or "fetching". The
        # device holds the result until it is asked for it, so "fetching" is
        # a real state that can be retried rather than a moment in a
        # transfer.
        self.stack_state = None
        self.stack_kind = None
        self.stack_started = 0.0
        self.stack_poll_countdown = 0
        self.stack_expected_s = 0.0
        self.stack_deadline = 0.0
        self.stack_fetch_tries = 0
        self.capture_status = None
        # The record behind whatever is on screen, kept so a saved file can
        # record how the numbers were produced. None when the display did not
        # come from a stack.
        self.stack_display = None

        # Card browser. sd_fetch holds the SdFile whose record has been asked
        # for, and is the switch that decides where an arriving record goes:
        # a stored file to the browser, anything else to the capture state
        # machine. Only one binary transfer can be in flight at a time, since
        # the device refuses a second, so one variable is enough to tell them
        # apart.
        self.sd_status = None
        self.sd_files = []
        self.sd_fetch = None
        self.sd_fetch_action = None   # "view" or a path to write
        self.sd_fetch_deadline = 0.0
        self.sd_listing = False       # a paged listing is in progress
        self.sd_dirty = True          # the listing is stale, refresh on show

        self.last_stats = None
        self.fps = 0.0
        self._fps_t = time.time()
        self._fps_n = 0
        self.peaks = []
        self.cursor_px = None

        # Zoom state. None means "home": the full pixel range and the
        # fixed 0 V to full-scale range from _plot_geom(). Set together so
        # a view is always a complete (x, y) pair rather than one axis
        # zoomed and the other not, which would be a confusing state to
        # be in and to draw.
        self.view_x = None
        self.view_y = None
        self._drag_start = None   # (canvas_x, canvas_y) of a Button-1 press
        self._drag_rect = None    # canvas id of the in-progress selection box
        self._drag_last = None    # (x0, y0, x1, y1) of the box, in canvas
                                   # coordinates; kept independently of the
                                   # canvas id above so the box can be
                                   # redrawn after a live-data redraw wipes
                                   # the canvas mid-drag (see _draw_drag_rect)
        # Below this, in canvas pixels, a press-drag-release is treated as
        # a click rather than a zoom selection, so the existing
        # click-to-set-calibration-pixel behaviour keeps working with an
        # unsteady hand.
        self._drag_threshold = 5

        # Before _build: widgets read the palette as they are created.
        _apply_palette(self.settings.get("theme", "light"))
        self._build()
        self._refresh_ports()
        # The panel shows a frame count derived from the exposure field, so
        # it has to be filled in once before anything is typed into either.
        self._update_stack_plan()
        self.root.after(POLL_MS, self._poll)

    # ---------------------------------------------------------------- UI

    def _build(self):
        global UI, UI_SEMI
        self.root.title(APP_NAME)
        self.root.configure(bg=BG)
        self.root.minsize(1180, 820)

        # Before any widget is made. A widget reads its font when it is
        # created, so a family chosen afterwards would reach only the ones
        # built later.
        UI, UI_SEMI, _mono = typography.resolve(self.root)
        typography.apply_named_fonts(self.root, UI, MONO)

        self._set_window_icon()

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        self._style = style
        self._apply_styles()

        self._build_menu()

        outer = ttk.Frame(self.root, padding=6)
        outer.pack(fill="both", expand=True)

        left = ttk.Frame(outer)
        left.pack(side="left", fill="y", padx=(0, 6))
        right = ttk.Frame(outer)
        right.pack(side="left", fill="both", expand=True)

        self._build_acquisition(left)
        self._build_stack(left)
        self._build_correction(left)
        self._build_readout(left)
        self._build_plot(right)
        self._build_tabs(right)

        # Prose, not a column of digits, so this one is the interface face.
        self.status = ttk.Label(self.root, text="Disconnected", anchor="w",
                                padding=(6, 3))
        self.status.pack(fill="x", side="bottom")

    def _set_window_icon(self):
        """The mark, in the title bar and on the taskbar.

        Windows is given the .ico. It holds a separately drawn image for
        every size the shell asks for, from the 16 px title bar to the
        256 px tile, so nothing is resampled. Everywhere else takes the
        PNG through iconphoto, which needs a live PhotoImage rather than a
        path, and needs it kept alive: a local would be collected and the
        icon would go blank. Both files come from tools/make_logo.py.
        """
        here = resources.package_dir()
        ico = here / "icon.ico"
        if sys.platform == "win32" and ico.exists():
            try:
                self.root.iconbitmap(default=str(ico))
                return
            except tk.TclError:
                pass

        png = here / "logo.png"
        if png.exists():
            try:
                self.app_icon = tk.PhotoImage(file=str(png))
                self.root.iconphoto(True, self.app_icon)
            except tk.TclError:
                pass

    def _build_menu(self):
        """The window menu.

        Everything here is a one-shot action or a setting that is changed
        rarely: opening a file, connecting, choosing a theme, moving the
        calibration between this computer and the instrument. Controls that
        are touched while measuring stay on the panels, where they can be
        reached without opening anything.

        There is no Identify item. The device identifies itself when the
        port opens, and again whenever it sends an "I " line, so a button
        asking it to do what it has already done is a button that can only
        confuse.
        """
        # tearoff=0 on the bar as well as on each menu. Left at the default,
        # Tk puts a dashed "tear this menu off" entry at index 0 of every one
        # of them, which is a Tk 8.0 habit and not something this window
        # wants.
        menubar = tk.Menu(self.root, tearoff=0)
        self.menubar = menubar

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Open spectrum...", accelerator="Ctrl+O",
                              command=self._open_spectrum_file)
        file_menu.add_command(label="Save spectrum as text...",
                              accelerator="Ctrl+S",
                              command=self._save_spectrum)
        file_menu.add_separator()
        file_menu.add_command(label="Load dark reference...",
                              command=lambda: self._load_ref("dark"))
        file_menu.add_command(label="Load flat reference...",
                              command=lambda: self._load_ref("flat"))
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.on_close)
        menubar.add_cascade(label="File", menu=file_menu)

        # The port list is rebuilt each time the menu is posted rather than
        # at build time. A device plugged in after the window opened would
        # otherwise never appear without a Rescan.
        device_menu = tk.Menu(menubar, tearoff=0,
                              postcommand=self._refresh_ports)
        self.device_menu = device_menu
        self.port_menu = tk.Menu(device_menu, tearoff=0)
        device_menu.add_cascade(label="Port", menu=self.port_menu)
        device_menu.add_command(label="Rescan ports",
                                command=self._refresh_ports)
        device_menu.add_separator()
        device_menu.add_command(label="Connect", command=self._toggle_connection)
        # Index of the Connect item, so its label can follow the state.
        self._connect_item = device_menu.index("end")
        menubar.add_cascade(label="Device", menu=device_menu)

        cal_menu = tk.Menu(menubar, tearoff=0)
        cal_menu.add_command(label="Save to device",
                             command=self._cal_push_to_device)
        cal_menu.add_command(label="Load from device",
                             command=self._cal_pull_from_device)
        cal_menu.add_command(label="Clear on device",
                             command=self._cal_clear_on_device)
        cal_menu.add_separator()
        cal_menu.add_command(label="Export to file...",
                             command=self._cal_export_file)
        cal_menu.add_command(label="Import from file...",
                             command=self._cal_import_file)
        menubar.add_cascade(label="Calibration", menu=cal_menu)

        view_menu = tk.Menu(menubar, tearoff=0)
        self.dark_var = tk.BooleanVar(value=(THEME == "dark"))
        view_menu.add_checkbutton(label="Dark mode", variable=self.dark_var,
                                  command=self._toggle_theme)
        view_menu.add_separator()
        view_menu.add_command(label="Reset zoom", command=self._reset_zoom)
        menubar.add_cascade(label="View", menu=view_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="Diagnostic legend",
                              command=self._show_legend)
        help_menu.add_separator()
        help_menu.add_command(label=f"About",
                              command=self._show_about)
        menubar.add_cascade(label="Help", menu=help_menu)

        self.root.config(menu=menubar)
        self.root.bind_all("<Control-o>", lambda e: self._open_spectrum_file())
        self.root.bind_all("<Control-s>", lambda e: self._save_spectrum())

    def _update_menu_state(self):
        """Keep the Connect item's label on the state it will move to."""
        if not hasattr(self, "device_menu"):
            return
        self.device_menu.entryconfigure(
            self._connect_item,
            label="Disconnect" if self.device.connected else "Connect")

    def _build_acquisition(self, parent):
        box = ttk.Labelframe(parent, text="Preview", padding=6)
        box.pack(fill="x", pady=(0, 6))

        # Kept for compatibility with saved workflows. FPS is the primary
        # field. Both visible fields represent the same frame period.
        self.rate_mode = tk.StringVar(value="hz")
        self.fps_var = tk.StringVar(value="40")
        self.exposure_var = tk.StringVar(value="25")
        # The unit sits to the RIGHT of each entry, not inside the label on
        # the left. "Integration (ms)" needed a label wider than the entry it
        # described, which squeezed the field it belonged to; with the unit
        # after the box, both names fit and both fields are the same width.
        row = ttk.Frame(box)
        row.pack(fill="x")
        ttk.Label(row, text="Rate", width=11).pack(side="left")
        self.fps_entry = ttk.Entry(row, textvariable=self.fps_var, width=10)
        self.fps_entry.pack(side="left")
        ttk.Label(row, text="fps", style="Dim.TLabel").pack(side="left",
                                                            padx=(4, 0))
        self.fps_entry.bind("<KeyRelease>", self._fps_changed)
        self.fps_entry.bind("<Return>", self._apply_exposure)

        row1b = ttk.Frame(box)
        row1b.pack(fill="x", pady=(3, 0))
        ttk.Label(row1b, text="Integration", width=11).pack(side="left")
        self.exposure_entry = ttk.Entry(
            row1b, textvariable=self.exposure_var, width=10)
        self.exposure_entry.pack(side="left")
        ttk.Label(row1b, text="ms", style="Dim.TLabel").pack(side="left",
                                                             padx=(4, 0))
        self.exposure_entry.bind("<KeyRelease>", self._integration_changed)
        self.exposure_entry.bind("<Return>", lambda e: self._apply_exposure())
        ttk.Button(row1b, text="Set", width=5,
                   command=self._apply_exposure).pack(side="right")

        # The preview is intentionally one frame. Long integrations and
        # rejection belong to Capture.
        self.avg_var = tk.StringVar(value="1")

        row3 = ttk.Frame(box)
        row3.pack(fill="x", pady=(6, 0))
        ttk.Label(row3, text="Frames").pack(side="left")
        self.capture_var = tk.StringVar(value="100")
        ttk.Entry(row3, textvariable=self.capture_var, width=6).pack(
            side="left", padx=4)
        ttk.Button(row3, text="Stack", width=9,
                   command=self._start_capture).pack(side="left")

        row4 = ttk.Frame(box)
        row4.pack(fill="x", pady=(4, 0))
        self.live_btn = ttk.Button(row4, text="Pause", width=9,
                                   command=self._toggle_live)
        self.live_btn.pack(side="left", padx=(0, 3))
        ttk.Button(row4, text="Save", width=9,
                   command=self._save_spectrum).pack(side="left", padx=3)

        self.capture_label = ttk.Label(box, text="", font=(MONO, 8))
        self.capture_label.pack(fill="x", pady=(4, 0))

    def _build_stack(self, parent):
        """Stacked capture, combined on the device.

        Separate from the Acquisition panel deliberately. Everything there
        acts on the live stream; this runs a fixed job that produces one
        result and stops. The exposure is taken from the Acquisition panel
        rather than repeated here, because it is the same physical quantity
        and two fields for it would drift apart.
        """
        box = ttk.Labelframe(parent, text="Capture",
                             padding=6)
        box.pack(fill="x", pady=(0, 6))

        row = ttk.Frame(box)
        row.pack(fill="x")
        ttk.Label(row, text="Duration (s)").pack(side="left")
        self.stack_secs_var = tk.StringVar(value="10")
        entry = ttk.Entry(row, textvariable=self.stack_secs_var, width=7)
        entry.pack(side="left", padx=4)
        entry.bind("<KeyRelease>", lambda e: self._update_stack_plan())

        row2 = ttk.Frame(box)
        row2.pack(fill="x", pady=(4, 0))
        ttk.Label(row2, text="Rejection sigma").pack(side="left")
        self.stack_sigma_var = tk.StringVar(value="3.0")
        ttk.Entry(row2, textvariable=self.stack_sigma_var,
                  width=5).pack(side="left", padx=(4, 10))
        ttk.Label(row2, text="Block").pack(side="left")
        self.stack_block_var = tk.StringVar(value="16")
        ttk.Combobox(row2, textvariable=self.stack_block_var, width=4,
                     state="readonly",
                     values=("4", "8", "16", "32")).pack(side="left", padx=4)

        row3 = ttk.Frame(box)
        row3.pack(fill="x", pady=(4, 0))
        ttk.Label(row3, text="Output").pack(side="left")
        self.stack_kind_var = tk.StringVar(value="spectrum")
        ttk.Combobox(row3, textvariable=self.stack_kind_var, width=10,
                     state="readonly",
                     values=("spectrum", "dark", "flat")).pack(side="left",
                                                               padx=4)

        row4 = ttk.Frame(box)
        row4.pack(fill="x", pady=(5, 0))
        self.stack_start_btn = ttk.Button(row4, text="Start", width=9,
                                          command=self._start_stack)
        self.stack_start_btn.pack(side="left")
        self.stack_stop_btn = ttk.Button(row4, text="Stop", width=9,
                                         command=self._stop_stack,
                                         state="disabled")
        self.stack_stop_btn.pack(side="left", padx=3)

        self.stack_label = ttk.Label(box, text="idle", font=(MONO, 8),
                                     justify="left")
        self.stack_label.pack(fill="x", pady=(5, 0))

    def _build_correction(self, parent):
        box = ttk.Labelframe(parent, text="Reference correction", padding=6)
        box.pack(fill="x", pady=(0, 6))

        self.use_dark = tk.BooleanVar(value=False)
        self.use_flat = tk.BooleanVar(value=False)
        # On by default: the end pixels are measured artifacts, not data.
        # See processing.mask_end_pixels.
        self.mask_ends = tk.BooleanVar(value=True)
        ttk.Checkbutton(box, text="Subtract dark", variable=self.use_dark,
                        command=self._redraw).pack(anchor="w")
        ttk.Checkbutton(box, text="Apply flat field", variable=self.use_flat,
                        command=self._redraw).pack(anchor="w")
        ttk.Checkbutton(box, text="Mask end pixels",
                        variable=self.mask_ends,
                        command=self._redraw).pack(anchor="w")

        row = ttk.Frame(box)
        row.pack(fill="x", pady=(4, 0))
        ttk.Button(row, text="Acquire dark", width=13,
                   command=lambda: self._start_capture("dark")).pack(side="left")
        ttk.Button(row, text="Load", width=6,
                   command=lambda: self._load_ref("dark")).pack(side="left",
                                                                padx=2)
        row2 = ttk.Frame(box)
        row2.pack(fill="x", pady=(3, 0))
        ttk.Button(row2, text="Acquire flat", width=13,
                   command=lambda: self._start_capture("flat")).pack(side="left")
        ttk.Button(row2, text="Load", width=6,
                   command=lambda: self._load_ref("flat")).pack(side="left",
                                                                padx=2)

        self.ref_label = ttk.Label(box, text="dark: none\nflat: none",
                                   font=(MONO, 8), justify="left")
        self.ref_label.pack(fill="x", pady=(5, 0))

    def _build_readout(self, parent):
        box = ttk.Labelframe(parent, text="Statistics", padding=6)
        box.pack(fill="x")
        self.stat_label = ttk.Label(box, text="no data", font=(MONO, 9),
                                    justify="left")
        self.stat_label.pack(fill="x")

    def _build_plot(self, parent):
        box = ttk.Frame(parent)
        box.pack(fill="x")

        width = PLOT_W + MARGIN_L + MARGIN_R
        self.strip = tk.Canvas(box, width=width, height=STRIP_H, bg=BG,
                               highlightthickness=0)
        self.strip.pack()
        self.strip_img = tk.PhotoImage(width=PLOT_W, height=STRIP_H)
        self.strip.create_image(MARGIN_L, 0, anchor="nw", image=self.strip_img)

        self.plot = tk.Canvas(box, width=width, height=PLOT_H, bg=BG,
                              highlightthickness=0)
        self.plot.pack()
        # Click-drag draws a selection box and zooms to it on release. A
        # drag shorter than _drag_threshold is treated as a plain click, so
        # the existing click-to-set-calibration-pixel behaviour is
        # unaffected. Right-click and the Home button both reset to the
        # full view; two ways in because a mouse gesture is fast once
        # known but a labelled button is what a first-time user finds.
        self.plot.bind("<Motion>", self._on_hover)
        self.plot.bind("<ButtonPress-1>", self._on_press)
        self.plot.bind("<B1-Motion>", self._on_drag)
        self.plot.bind("<ButtonRelease-1>", self._on_release)
        self.plot.bind("<Button-3>", lambda e: self._reset_zoom())
        self.plot.bind("<Leave>", lambda e: self._set_cursor(None))

        info_row = ttk.Frame(box)
        info_row.pack(fill="x")
        self.hover_label = ttk.Label(info_row, text="", font=(MONO, 9),
                                     anchor="w")
        self.hover_label.pack(side="left", fill="x", expand=True)
        self.zoom_label = ttk.Label(info_row, text="", font=(MONO, 9),
                                    style="Dim.TLabel", anchor="e")
        self.zoom_label.pack(side="right")
        self.home_btn = ttk.Button(info_row, text="Home", width=6,
                                   command=self._reset_zoom, state="disabled")
        self.home_btn.pack(side="right", padx=(0, 8))

    def _apply_styles(self):
        """Push the current palette into ttk, including interaction states.

        Setting only ``foreground`` is not enough with the clam theme: its
        buttons and fields otherwise retain their light-theme backgrounds,
        and clam supplies a nearly white disabled foreground. Keep every
        state explicit so switching themes does not leave stale colours.
        """
        st = self._style
        # The root style carries the interface font, so every ttk widget
        # that is not given one of its own gets it. The readouts and the
        # console are given one of their own, and keep the monospace face.
        st.configure(".", background=BG, foreground=FG,
                     fieldbackground=INPUT, bordercolor=BORDER,
                     lightcolor=BORDER, darkcolor=BORDER,
                     font=(UI, typography.UI_SIZE))
        # A hairline in BORDER_SOFT, not the heavy three-dimensional edge
        # clam draws by default. A group frame is there to say where a group
        # ends; anything stronger competes with the trace for attention.
        st.configure("TLabelframe", background=BG, foreground=FG,
                     relief="solid", borderwidth=1,
                     bordercolor=BORDER_SOFT, lightcolor=BORDER_SOFT,
                     darkcolor=BORDER_SOFT)
        st.configure("TLabelframe.Label", background=BG, foreground=FG_DIM,
                     font=(UI_SEMI, typography.UI_SIZE))
        st.configure("TFrame", background=BG)
        st.configure("TLabel", background=BG, foreground=FG)
        st.configure("Dim.TLabel", background=BG, foreground=FG_DIM)

        for name in ("TCheckbutton", "TRadiobutton"):
            st.configure(name, background=BG, foreground=FG,
                         indicatorbackground=INPUT,
                         indicatorforeground=FG,
                         bordercolor=BORDER)
            st.map(name,
                   background=[("active", BG)],
                   foreground=[("disabled", DISABLED_FG), ("active", FG)],
                   indicatorbackground=[("selected", SELECT),
                                        ("active", CONTROL_HOVER)],
                   indicatorforeground=[("selected", "#ffffff")])

        st.configure("TButton", background=CONTROL, foreground=FG,
                     padding=(7, 4), borderwidth=0, relief="flat")
        st.map("TButton",
               background=[("disabled", PANEL),
                           ("pressed", CONTROL_PRESSED),
                           ("active", CONTROL_HOVER)],
               foreground=[("disabled", DISABLED_FG),
                           ("pressed", FG), ("active", FG)],
               relief=[("pressed", "sunken")])

        st.configure("TEntry", fieldbackground=INPUT, foreground=FG,
                     bordercolor=BORDER, lightcolor=BORDER,
                     darkcolor=BORDER, insertcolor=FG, padding=3)
        st.map("TEntry",
               fieldbackground=[("disabled", PANEL), ("readonly", PANEL)],
               foreground=[("disabled", DISABLED_FG)],
               bordercolor=[("focus", SELECT)])

        st.configure("TCombobox", fieldbackground=INPUT, background=CONTROL,
                     foreground=FG, arrowcolor=FG_DIM, bordercolor=BORDER,
                     lightcolor=BORDER, darkcolor=BORDER, padding=2)
        st.map("TCombobox",
               fieldbackground=[("readonly", INPUT), ("disabled", PANEL)],
               background=[("active", CONTROL_HOVER),
                           ("pressed", CONTROL_PRESSED)],
               foreground=[("readonly", FG), ("disabled", DISABLED_FG)],
               arrowcolor=[("disabled", DISABLED_FG), ("active", FG)],
               bordercolor=[("focus", SELECT)])

        st.configure("TNotebook", background=BG, borderwidth=0,
                     bordercolor=BORDER_SOFT, lightcolor=BORDER_SOFT,
                     darkcolor=BORDER_SOFT, tabmargins=(0, 0, 0, 0))
        st.configure("TNotebook.Tab", background=CONTROL, foreground=FG_DIM,
                     bordercolor=BORDER_SOFT, lightcolor=BORDER_SOFT,
                     darkcolor=BORDER_SOFT, padding=(12, 5),
                     font=(UI_SEMI, typography.UI_SIZE))
        st.map("TNotebook.Tab",
               background=[("selected", PANEL), ("active", CONTROL_HOVER)],
               foreground=[("selected", FG), ("active", FG)],
               expand=[("selected", (0, 0, 0, 1))])

        st.configure("Treeview", background=PANEL, fieldbackground=PANEL,
                     foreground=FG, borderwidth=1, relief="solid",
                     bordercolor=BORDER_SOFT, lightcolor=BORDER_SOFT,
                     darkcolor=BORDER_SOFT, rowheight=22)
        st.map("Treeview", background=[("selected", SELECT)],
               foreground=[("selected", "#ffffff")])
        st.configure("Treeview.Heading", background=CONTROL, foreground=FG,
                     bordercolor=BORDER_SOFT, relief="flat",
                     font=(UI_SEMI, typography.UI_SIZE))
        st.map("Treeview.Heading", background=[("active", CONTROL_HOVER)])

        st.configure("TScrollbar", background=CONTROL, troughcolor=BG,
                     bordercolor=BG, arrowcolor=FG_DIM)
        st.map("TScrollbar", background=[("active", CONTROL_HOVER),
                                         ("pressed", CONTROL_PRESSED)],
               arrowcolor=[("active", FG)])

        # The drop-down portion of a ttk Combobox is a classic Tk Listbox,
        # so ttk styles cannot reach it. These defaults also make selection
        # consistent in the Text widgets.
        # A menu is a classic Tk widget even where the platform draws it,
        # so it takes its font from the option database rather than from a
        # ttk style. Set before the menus are built.
        self.root.option_add("*Menu.font", (UI, typography.UI_SIZE))
        self.root.option_add("*TCombobox*Listbox.font",
                             (UI, typography.UI_SIZE))
        self.root.option_add("*TCombobox*Listbox.background", INPUT)
        self.root.option_add("*TCombobox*Listbox.foreground", FG)
        self.root.option_add("*TCombobox*Listbox.selectBackground", SELECT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        self.root.option_add("*selectBackground", SELECT)
        self.root.option_add("*selectForeground", "#ffffff")

    def _toggle_theme(self):
        self._set_theme("dark" if self.dark_var.get() else "light")

    def _set_theme(self, name):
        """Switch palette and repaint.

        Anything that reads a colour global when it draws picks the new
        palette up from the redraw at the end. The widgets handled here are
        the ones given a colour when they were built, which do not re-read
        it.
        """
        _apply_palette(name)
        self.settings.set("theme", name)
        self.settings.save()

        self._apply_styles()
        self.root.configure(bg=BG)
        for canvas in (self.strip, self.plot, self.hist_canvas):
            canvas.configure(bg=BG)
        for text in (self.peak_text, self.cal_text):
            text.configure(bg=PANEL, fg=FG, selectbackground=SELECT,
                           selectforeground="#ffffff")
        self.diag_text.configure(bg=PANEL, fg=FG, insertbackground=FG,
                                 selectbackground=SELECT,
                                 selectforeground="#ffffff")
        self.cmd_entry.configure(bg=INPUT, fg=FG, insertbackground=FG,
                                 selectbackground=SELECT,
                                 selectforeground="#ffffff")
        for tag, style in LOG_STYLES.items():
            self.diag_text.tag_config(tag, **style)
        self.sd_tree.tag_configure("damaged", foreground=MISSING)

        # The About box takes its colours when it is built and has no way
        # to follow a change, so it is rebuilt rather than left stale.
        about = getattr(self, "_about_win", None)
        if about is not None and about.winfo_exists():
            about.destroy()
            self._show_about()

        # Labels whose colour is set on refresh rather than at build time.
        self._update_ref_label()
        self._update_stack_plan()
        self._redraw()
        self._update_pixel_hist()

    def _build_tabs(self, parent):
        nb = ttk.Notebook(parent)
        self.tabs = nb
        nb.pack(fill="both", expand=True, pady=(6, 0))
        # Selecting the Console puts the caret in its command line. A
        # terminal you have to click into before typing is a terminal you
        # mistype the first command into.
        nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        cal = ttk.Frame(nb, padding=6)
        nb.add(cal, text="Calibration")
        self._build_cal_tab(cal)

        dist = ttk.Frame(nb, padding=6)
        nb.add(dist, text="Distribution")
        self._build_dist_tab(dist)

        peaks = ttk.Frame(nb, padding=6)
        nb.add(peaks, text="Peaks")
        self.peak_text = tk.Text(peaks, height=9, font=(MONO, 9),
                                 bg=PANEL, fg=FG, wrap="none")
        self.peak_text.pack(fill="both", expand=True)
        prow = ttk.Frame(peaks)
        prow.pack(fill="x", pady=(4, 0))
        ttk.Label(prow, text="Min prominence (V)").pack(side="left")
        self.prom_var = tk.StringVar(value="0.005")
        ttk.Entry(prow, textvariable=self.prom_var, width=8).pack(side="left",
                                                                  padx=4)

        store = ttk.Frame(nb, padding=6)
        nb.add(store, text="Files")
        self.storage_tab = store
        self._build_storage_tab(store)

        diag = ttk.Frame(nb, padding=6)
        nb.add(diag, text="Console")
        self.console_tab = diag
        self._build_console_tab(diag)

    def _on_tab_changed(self, event=None):
        if getattr(self, "console_tab", None) is None:
            return
        current = self.tabs.select()
        if current == str(self.console_tab):
            self.cmd_entry.focus_set()
        elif current == str(getattr(self, "storage_tab", None)):
            # Refresh on arrival, but only when something has happened since
            # the listing was last read. Every capture writes a file, so a
            # listing that is never refreshed is wrong within one capture,
            # and one that refreshes on every tab click adds serial traffic
            # to the live view for nothing.
            if self.sd_dirty and self.device.connected:
                self._sd_refresh()

    def _build_storage_tab(self, parent):
        """The card browser.

        Everything shown here is read from the card, not remembered from
        this session. The device reads each file's own header to build the
        listing, so a card carried over from another instrument, or files
        written before this program was started, appear exactly like the
        ones written a minute ago.
        """
        parent.rowconfigure(2, weight=1)
        parent.columnconfigure(0, weight=1)

        top = ttk.Frame(parent)
        top.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        ttk.Button(top, text="Refresh", command=self._sd_refresh).pack(
            side="left")
        ttk.Button(top, text="Mount", command=self._sd_mount).pack(
            side="left", padx=(4, 0))
        self.sd_auto_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="Store every capture",
                        variable=self.sd_auto_var,
                        command=self._sd_toggle_auto).pack(side="left",
                                                           padx=(12, 0))

        # Lossless, both of them. "rle" transposes the pixel array into byte
        # planes and run-length codes it, which is about 1.6 to 1 on a real
        # spectrum; "none" writes the record as it stands. The setting
        # applies to NEW files only. Every file records the codec it was
        # written with, so changing this never makes an older one unreadable.
        ttk.Label(top, text="Codec").pack(side="left", padx=(12, 2))
        self.sd_codec_var = tk.StringVar(value="rle")
        codec_box = ttk.Combobox(top, textvariable=self.sd_codec_var,
                                 values=sorted(CODEC_NAMES.values()),
                                 width=6, state="readonly")
        codec_box.pack(side="left")
        codec_box.bind("<<ComboboxSelected>>", self._sd_set_codec)
        self.sd_label = ttk.Label(top, text="not connected",
                                  style="Dim.TLabel")
        self.sd_label.pack(side="right")

        # Filters the listing already in hand. It does not ask the device
        # again: the whole directory is read on refresh, so searching is a
        # local operation and stays instant while a capture is running.
        search_row = ttk.Frame(parent)
        search_row.grid(row=1, column=0, columnspan=2, sticky="ew",
                        pady=(0, 4))
        ttk.Label(search_row, text="Search").pack(side="left")
        self.sd_search_var = tk.StringVar()
        search = ttk.Entry(search_row, textvariable=self.sd_search_var,
                           width=24)
        search.pack(side="left", padx=(6, 0))
        search.bind("<KeyRelease>", lambda _e: self._sd_fill_tree())
        ttk.Button(search_row, text="Clear", width=6,
                   command=self._sd_clear_search).pack(side="left", padx=(4, 0))
        self.sd_count_label = ttk.Label(search_row, text="",
                                        style="Dim.TLabel")
        self.sd_count_label.pack(side="right")

        cols = ("name", "date", "kind", "frames", "exposure", "stored")
        tree = ttk.Treeview(parent, columns=cols, show="headings",
                            selectmode="browse", height=12)
        self.sd_tree = tree
        # Which column the listing is ordered by, and which way. Sequence
        # descending is the default because the file just written is the one
        # most likely to be wanted, and the sequence number orders reliably
        # where the date does not: anything written before the host handed
        # the device a clock is dated 2000-01-01.
        self.sd_sort_key = "seq"
        self.sd_sort_reverse = True
        for key, title, width, anchor in (
                ("name", "File", 110, "w"),
                ("date", "Taken", 150, "w"),
                ("kind", "Kind", 80, "w"),
                ("frames", "Frames", 70, "e"),
                ("exposure", "Exposure", 90, "e"),
                ("stored", "Stored", 120, "e")):
            tree.heading(key, text=title,
                         command=lambda k=key: self._sd_sort_by(k))
            tree.column(key, width=width, anchor=anchor, stretch=(key == "date"))
        tree.grid(row=2, column=0, sticky="nsew")
        # Double-click is the same as View. It is what a list of files
        # invites, and a browser that does nothing on it feels broken.
        tree.bind("<Double-1>", lambda _e: self._sd_view())
        # F2 renames, as it does in a file manager.
        tree.bind("<F2>", lambda _e: self._sd_rename())

        bar = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
        bar.grid(row=2, column=1, sticky="ns")
        tree.configure(yscrollcommand=bar.set)

        # A file whose header could not be read is still listed, so that it
        # can be deleted, but it is marked and cannot be viewed.
        tree.tag_configure("damaged", foreground=MISSING)

        row = ttk.Frame(parent)
        row.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(row, text="View", command=self._sd_view).pack(side="left")
        ttk.Button(row, text="Rename...", command=self._sd_rename).pack(
            side="left", padx=(4, 0))
        ttk.Button(row, text="Save as text...",
                   command=self._sd_download).pack(side="left", padx=(4, 0))
        ttk.Button(row, text="Delete", command=self._sd_delete).pack(
            side="left", padx=(4, 0))
        self.sd_busy_label = ttk.Label(row, text="", style="Dim.TLabel")
        self.sd_busy_label.pack(side="right")

    # ------------------------------------------------------------- storage

    def _sd_supported(self):
        if not self.device.connected:
            messagebox.showwarning(APP_NAME, "Connect to a device first.")
            return False
        if not self.device.info.supports_storage:
            messagebox.showwarning(
                APP_NAME,
                "This firmware has no SD card support.\n\n"
                "Update it, or save spectra from the plot instead.")
            return False
        return True

    def _sd_refresh(self):
        """Start a listing. Pages arrive through _poll_once."""
        if not self.device.connected:
            return
        if not self.device.info.supports_storage:
            return
        self.sd_files = []
        self.sd_listing = True
        self.sd_dirty = False
        try:
            # free=1 walks the whole file allocation table on the device,
            # which takes seconds on a large card. Asked for here and nowhere
            # else: a refresh is a deliberate act, while the status poll that
            # runs during a capture is not, and how full the card is cannot
            # be shown without it.
            self.device.sd_status(free=True)
            self.device.sd_list(0, SD_PAGE)
        except DeviceError as exc:
            self.sd_listing = False
            self._log(f"[!] card listing failed: {exc}")

    def _sd_mount(self):
        if not self._sd_supported():
            return
        try:
            self.device.sd_mount()
        except DeviceError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        self.sd_dirty = True
        self._log("card mount requested")

    def _sd_set_codec(self, event=None):
        if not self.device.connected:
            return
        try:
            self.device.sd_set_codec(self.sd_codec_var.get())
            self.device.sd_status()
        except DeviceError as exc:
            self._log(f"[!] {exc}")

    def _sd_toggle_auto(self):
        if not self.device.connected:
            return
        try:
            self.device.sd_set_auto(self.sd_auto_var.get())
        except DeviceError as exc:
            self._log(f"[!] {exc}")

    def _on_sd_event(self, kind, payload):
        if kind == "file":
            self.sd_files.append(payload)
        elif kind == "list_end":
            shown = payload["shown"]
            nxt = payload["start"] + shown
            # shown == 0 ends the walk even if the total says otherwise. A
            # page that returns nothing while claiming more exist would
            # otherwise request the same page forever.
            if shown > 0 and nxt < payload["total"]:
                try:
                    self.device.sd_list(nxt, SD_PAGE)
                    return
                except DeviceError:
                    pass
            self.sd_listing = False
            self._sd_fill_tree()
        elif kind == "status":
            self.sd_status = payload
            self.sd_auto_var.set(payload.auto)
            if payload.codec_name in CODEC_NAMES.values():
                self.sd_codec_var.set(payload.codec_name)
            self._sd_update_label()
        elif kind == "saved":
            self.sd_dirty = True
            self._log(f"stored on card as {payload}")
            # Ask again, so the label and the picker show what the write
            # actually cost rather than what was expected of it.
            try:
                self.device.sd_status()
            except DeviceError:
                pass
        elif kind == "deleted":
            self.sd_dirty = True
            self._log(f"deleted {payload} from the card")
            self._sd_refresh()
        elif kind == "renamed":
            self.sd_dirty = True
            self._log(f"renamed {payload['name']} to {payload['to']}")
            self._sd_refresh()

    def _sd_update_label(self):
        if not hasattr(self, "sd_label"):
            return
        if not self.device.connected:
            self.sd_label.config(text="not connected", foreground=FG_DIM)
            return
        st = self.sd_status
        if st is None:
            self.sd_label.config(text="card state unknown", foreground=FG_DIM)
        elif not st.mounted:
            self.sd_label.config(text=f"no card ({st.error})",
                                 foreground=MISSING)
        else:
            self.sd_label.config(text=st.summary(), foreground=FG_DIM)

    @staticmethod
    def _sd_seq_of(entry):
        """Sequence number encoded in a file name, or 0.

        The two letter tag comes first, so a plain string sort groups by
        kind and interleaves the dates. The sequence number is also more
        trustworthy than the file date: it always increases, while the date
        is 2000-01-01 for anything written before the host handed the device
        a clock. A renamed file has no sequence number and sorts as 0.
        """
        digits = entry.name[2:8]
        return int(digits) if digits.isdigit() else 0

    def _sd_sort_by(self, key):
        """Order the listing by one column. Clicking it again reverses."""
        if self.sd_sort_key == key:
            self.sd_sort_reverse = not self.sd_sort_reverse
        else:
            self.sd_sort_key = key
            # Names and kinds read best A to Z; everything else is a
            # quantity, where the largest or the newest is what is wanted.
            self.sd_sort_reverse = key not in ("name", "kind")
        self._sd_fill_tree()

    def _sd_clear_search(self):
        self.sd_search_var.set("")
        self._sd_fill_tree()

    def _sd_visible_files(self):
        """The listing after the search box and the sort are applied."""
        needle = self.sd_search_var.get().strip().lower()
        files = list(self.sd_files)
        if needle:
            def hit(f):
                fields = (f.name, f.kind if f.valid else "unreadable",
                          f.date or "", f.tag)
                return any(needle in str(x).lower() for x in fields)
            files = [f for f in files if hit(f)]

        keys = {
            "seq": self._sd_seq_of,
            "name": lambda f: f.name.lower(),
            "date": lambda f: (f.date or ""),
            "kind": lambda f: (f.kind if f.valid else "unreadable"),
            "frames": lambda f: f.frames_stacked,
            "exposure": lambda f: f.exposure_us,
            "stored": lambda f: f.bytes,
        }
        key = keys.get(self.sd_sort_key, self._sd_seq_of)
        # Sequence second, so files that tie on the chosen column still come
        # out in a stable and meaningful order rather than in card order.
        files.sort(key=lambda f: (key(f), self._sd_seq_of(f)),
                   reverse=self.sd_sort_reverse)
        return files

    def _sd_fill_tree(self):
        tree = self.sd_tree
        tree.delete(*tree.get_children())
        files = self._sd_visible_files()
        for f in files:
            exposure = (f"{f.exposure_us / 1000.0:.2f} ms"
                        if f.exposure_us else "-")
            size = f"{f.bytes / 1024.0:.1f} kB"
            if f.ratio > 1.01:
                size += f" ({f.ratio:.2f}x)"
            date = f.date.replace("T", "  ") if f.date else "-"
            tags = () if f.valid else ("damaged",)
            kind = f.kind if f.valid else "unreadable"
            tree.insert("", "end", iid=f.name, tags=tags,
                        values=(f.name, date, kind,
                                f.frames_stacked or "-", exposure, size))

        # Which column is ordering the list, and which way, marked on the
        # heading itself. Without it a sorted list looks like an arbitrary
        # one.
        arrow = " v" if self.sd_sort_reverse else " ^"
        for key, title in (("name", "File"), ("date", "Taken"),
                           ("kind", "Kind"), ("frames", "Frames"),
                           ("exposure", "Exposure"), ("stored", "Stored")):
            tree.heading(key, text=title + (arrow if key == self.sd_sort_key
                                            else ""))

        total = len(self.sd_files)
        if len(files) == total:
            self.sd_count_label.config(text=f"{total} files")
        else:
            self.sd_count_label.config(text=f"{len(files)} of {total} files")
        self._sd_update_label()

    def _sd_selected(self):
        sel = self.sd_tree.selection()
        if not sel:
            messagebox.showinfo(APP_NAME, "Select a file first.")
            return None
        for f in self.sd_files:
            if f.name == sel[0]:
                return f
        return None

    def _sd_begin_fetch(self, entry, action):
        """Ask for one stored file and say where its record should go."""
        if not self._sd_supported():
            return
        if not entry.valid:
            messagebox.showwarning(
                APP_NAME,
                f"{entry.name} does not hold a readable record. It can be "
                f"deleted, but not read.")
            return
        if self.sd_fetch is not None:
            messagebox.showinfo(APP_NAME, "A file is already being read.")
            return
        if self.stack_state is not None:
            messagebox.showwarning(
                APP_NAME, "A capture is running. Wait for it to finish.")
            return
        try:
            self.device.sd_get(entry.name)
        except DeviceError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        self.sd_fetch = entry
        self.sd_fetch_action = action
        self.sd_fetch_deadline = time.time() + SD_FETCH_TIMEOUT_S
        self.sd_busy_label.config(text=f"reading {entry.name}...")

    def _sd_view(self):
        entry = self._sd_selected()
        if entry is not None:
            self._sd_begin_fetch(entry, "view")

    def _sd_download(self):
        entry = self._sd_selected()
        if entry is None:
            return
        stem = entry.name.rsplit(".", 1)[0]
        path = filedialog.asksaveasfilename(
            title=f"Save {entry.name}", defaultextension=".txt",
            filetypes=[("Text", "*.txt"), ("All files", "*.*")],
            initialfile=f"{stem}.txt")
        if not path:
            return
        self._sd_begin_fetch(entry, path)

    def _sd_delete(self):
        entry = self._sd_selected()
        if entry is None or not self._sd_supported():
            return
        if not messagebox.askyesno(
                APP_NAME,
                f"Delete {entry.name} from the card?\n\n"
                f"This cannot be undone."):
            return
        try:
            self.device.sd_delete(entry.name)
        except DeviceError as exc:
            messagebox.showerror(APP_NAME, str(exc))

    def _sd_rename(self):
        """Give a stored file a different name.

        The card is FAT, so the name has to be a plain 8.3 name; the device
        checks that too and refuses anything else. The extension is fixed
        here rather than typed, because a spectrum that is not called .STK
        stops appearing in the listing at all.

        Renaming loses the sequence number the name encodes. Nothing depends
        on it: the capture id inside the record is the identity, and the
        device reads the directory to decide where to continue numbering.
        """
        entry = self._sd_selected()
        if entry is None or not self._sd_supported():
            return
        stem = entry.name.rsplit(".", 1)[0]
        new = simpledialog.askstring(
            APP_NAME, f"New name for {entry.name}\n\n"
                      f"Up to 8 characters: A-Z, 0-9, - and _",
            initialvalue=stem, parent=self.root)
        if new is None:
            return
        new = new.strip().upper()
        if new.endswith(".STK"):
            new = new[:-4]
        if not new or len(new) > 8 or not all(
                c.isdigit() or ("A" <= c <= "Z") or c in "-_" for c in new):
            messagebox.showwarning(
                APP_NAME,
                "A name is 1 to 8 characters, from A-Z, 0-9, - and _.")
            return
        target = f"{new}.STK"
        if target == entry.name:
            return
        if any(f.name == target for f in self.sd_files):
            messagebox.showwarning(APP_NAME,
                                   f"{target} is already on the card.")
            return
        try:
            self.device.sd_rename(entry.name, target)
        except DeviceError as exc:
            messagebox.showerror(APP_NAME, str(exc))

    def _sd_fetch_failed(self, reason):
        name = self.sd_fetch.name if self.sd_fetch else "the file"
        self.sd_fetch = None
        self.sd_fetch_action = None
        self.sd_busy_label.config(text="")
        self._log(f"[!] reading {name} failed: {reason}")
        messagebox.showwarning(APP_NAME, f"Could not read {name}: {reason}")

    def _finish_sd_fetch(self, res):
        """A stored record arrived. Show it, or write it out."""
        entry = self.sd_fetch
        action = self.sd_fetch_action
        self.sd_fetch = None
        self.sd_fetch_action = None
        self.sd_busy_label.config(text="")
        if entry is None:
            return

        volts = self._stack_to_volts(res)
        if action == "view":
            # Shown the same way a fresh capture is: frozen, with the record
            # attached, so Save Spectrum from the plot writes the same header
            # it would write for a capture taken now.
            self.frozen = volts
            self.stack_display = res
            self._log(f"{entry.name}: " + res.summary())
            self._update_ref_label()
            self._redraw()
            return

        wl = self.cal.axis(len(volts))
        try:
            export.write_spectrum(
                path=action, pixels=np.arange(len(volts)), volts=volts,
                wavelengths=wl, info=self.device.info,
                frames=res.frames_stacked,
                elapsed_s=res.duration_ms / 1000.0,
                dark_applied=False, flat_applied=False,
                notch_applied=True,
                calibration=self.cal, peaks=None, stack=res,
                note=f"device_card:{entry.name}")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Save failed: {exc}")
            return
        self._log(f"saved {Path(action).name} from {entry.name}")

    def _stack_to_volts(self, res):
        """Codes to volts, with the corrections that do not need a reference.

        Dark and flat are deliberately NOT applied. A file read off the card
        is being examined, not measured, and the references loaded now are
        not necessarily the ones that belong to it. What is applied is the
        notch and the end mask, which depend on nothing outside the record.
        """
        info = self.device.info
        codes = np.array(res.mean, dtype=np.float64)
        codes[res.accepted <= 0] = np.nan
        v = processing.codes_to_volts(codes, info.vref_mv, info.full_scale)
        v = processing.notch_pattern(v)
        if self.mask_ends.get():
            v = processing.mask_end_pixels(v)
        return v

    def _build_console_tab(self, parent):
        """Diagnostic log and command line against the connected device.

        The log and the input share one panel deliberately. A command and
        the reply it produced belong in one scrollback in the order they
        happened, and the device answers on the same stream the frame
        stats arrive on, so separating them would only hide which line
        answered which command.
        """
        # Layout by grid, not pack: the log must absorb every spare pixel
        # while the input row keeps its height at any window size.
        parent.rowconfigure(1, weight=1)
        parent.columnconfigure(0, weight=1)

        # What the device said about itself. It sits here rather than beside
        # the acquisition controls because it is read when something is
        # wrong, which is what this tab is for, and it is never adjusted.
        self.dev_label = ttk.Label(parent, text="no device",
                                   font=(MONO, 8), justify="left",
                                   style="Dim.TLabel")
        self.dev_label.grid(row=0, column=0, sticky="w", pady=(0, 4))

        logbox = ttk.Frame(parent)
        logbox.grid(row=1, column=0, sticky="nsew")
        logbox.rowconfigure(0, weight=1)
        logbox.columnconfigure(0, weight=1)
        self.diag_text = tk.Text(logbox, height=9, font=(MONO, 8),
                                 bg=PANEL, fg=FG, wrap="none",
                                 insertbackground=FG)
        self.diag_text.grid(row=0, column=0, sticky="nsew")
        dscroll = ttk.Scrollbar(logbox, command=self.diag_text.yview)
        dscroll.grid(row=0, column=1, sticky="ns")
        self.diag_text.configure(yscrollcommand=dscroll.set)
        for name, style in LOG_STYLES.items():
            self.diag_text.tag_config(name, **style)
        # Read only to the keyboard, but still selectable and copyable.
        # Typing goes to the entry below instead of being swallowed here.
        self.diag_text.bind("<Key>", self._console_text_key)

        prompt = ttk.Frame(parent)
        prompt.grid(row=2, column=0, sticky="ew", pady=(4, 0))
        prompt.columnconfigure(1, weight=1)
        tk.Label(prompt, text=">", font=(MONO, 10, "bold"),
                 bg=BG, fg=TRACE).grid(row=0, column=0, padx=(0, 4))
        self.cmd_var = tk.StringVar()
        self.cmd_entry = tk.Entry(prompt, textvariable=self.cmd_var,
                                  font=(MONO, 9), bg=INPUT, fg=FG,
                                  insertbackground=FG,
                                  relief="solid", borderwidth=1)
        self.cmd_entry.grid(row=0, column=1, sticky="ew")
        self.cmd_entry.bind("<Return>", self._console_submit)
        self.cmd_entry.bind("<Up>", self._console_history_back)
        self.cmd_entry.bind("<Down>", self._console_history_forward)
        self.cmd_entry.bind("<Tab>", self._console_complete)
        self.cmd_entry.bind("<Control-l>", self._console_clear_event)
        ttk.Button(prompt, text="Send", width=7,
                   command=self._console_submit).grid(row=0, column=2,
                                                      padx=(4, 0))

        # Command history is a ring the Up key walks back through.
        # cmd_index sits one past the end when nothing is being recalled.
        self.cmd_history = []
        self.cmd_index = 0
        self.cmd_draft = ""
        self.console_records = deque(maxlen=2000)

        drow = ttk.Frame(parent)
        drow.grid(row=3, column=0, sticky="ew", pady=(4, 0))
        self.show_stats = tk.BooleanVar(value=False)
        self.show_replies = tk.BooleanVar(value=True)
        self.show_errors = tk.BooleanVar(value=True)
        self.show_status = tk.BooleanVar(value=True)
        self.show_commands = tk.BooleanVar(value=True)
        for label, variable in (("Frames", self.show_stats),
                                ("Replies", self.show_replies),
                                ("Errors", self.show_errors),
                                ("Status", self.show_status),
                                ("Commands", self.show_commands)):
            ttk.Checkbutton(drow, text=label,
                            variable=variable,
                            command=self._render_console).pack(side="left")
        ttk.Button(drow, text="Copy", width=7,
                   command=self._copy_diag).pack(side="left", padx=(6, 0))
        ttk.Button(drow, text="Clear", width=7,
                   command=self._console_clear).pack(side="left", padx=3)
        ttk.Button(drow, text="Commands", width=11,
                   command=self._console_help).pack(side="left", padx=3)
        ttk.Button(drow, text="Fields", width=8,
                   command=self._show_legend).pack(side="left", padx=3)

        self.diag_summary = ttk.Label(parent, text="", font=(MONO, 8),
                                      justify="left")
        self.diag_summary.grid(row=4, column=0, sticky="ew", pady=(4, 0))

    def _build_dist_tab(self, parent):
        top = ttk.Frame(parent)
        top.pack(fill="x")
        ttk.Button(top, text="Clear history", width=13,
                   command=self._clear_pixel_history).pack(side="right")

        view_row = ttk.Frame(parent)
        view_row.pack(fill="x", pady=(4, 0))
        self.hist_view = tk.StringVar(value="hist")
        # Two distinct spectra, not one, because they slice the data on
        # different axes with different physical limits.
        #
        # Per-pixel: one clicked pixel's value across many frames. One
        # sample per frame, so it is capped at half the frame rate, a few
        # tens of Hz on this instrument. Needs a clicked pixel because it
        # is fundamentally a property of that one pixel.
        #
        # Per-frame: the within-frame video signal across pixel number,
        # at the 50 kHz pixel readout rate, reaching into the kHz range
        # where the interferer in ISSUES.md entry 31 actually lives. Uses
        # the whole frame and does not need a clicked pixel: the
        # interferer is uniform across the array (entry 31), so there is
        # no location to select, and using the whole frame rather than a
        # window around one pixel also gives better frequency resolution.
        #
        # See APPLICATION.md section 8.
        for label, val in (("Histogram", "hist"),
                           ("Pixel FFT", "spectrum_time"),
                           ("Frame FFT", "spectrum_space")):
            ttk.Radiobutton(view_row, text=label, value=val,
                            variable=self.hist_view,
                            command=self._update_pixel_hist).pack(
                                side="left", padx=(0 if val == "hist" else 10, 0))

        self.hist_label = ttk.Label(parent, text="No pixel",
                                    font=(MONO, 9), justify="left")
        self.hist_label.pack(fill="x", pady=(6, 2))

        self.hist_canvas = tk.Canvas(parent, width=HIST_W, height=HIST_H,
                                     bg=BG, highlightthickness=0)
        self.hist_canvas.pack(fill="both", expand=True)

    def _build_cal_tab(self, parent):
        top = ttk.Frame(parent)
        top.pack(fill="x")
        ttk.Label(top, text="Pixel").pack(side="left")
        self.cal_px = tk.StringVar()
        self.cal_wl = tk.StringVar()
        ttk.Entry(top, textvariable=self.cal_px, width=8).pack(side="left",
                                                               padx=4)
        ttk.Label(top, text="Wavelength (nm)").pack(side="left")
        ttk.Entry(top, textvariable=self.cal_wl, width=10).pack(side="left",
                                                                padx=4)
        ttk.Button(top, text="Add", width=6,
                   command=self._add_cal_point).pack(side="left")
        ttk.Button(top, text="Remove", width=8,
                   command=self._remove_cal_point).pack(side="left", padx=3)
        ttk.Button(top, text="Clear all", width=9,
                   command=self._clear_cal).pack(side="left")

        # The same two actions are on the Calibration menu. They are here as
        # well because this is the tab someone is looking at while building a
        # calibration, and sending it to the instrument is the last step of
        # that job, not a separate one.
        store_row = ttk.Frame(parent)
        store_row.pack(fill="x", pady=(6, 0))
        ttk.Button(store_row, text="Save to device", width=15,
                   command=self._cal_push_to_device).pack(side="left")
        ttk.Button(store_row, text="Load from device", width=16,
                   command=self._cal_pull_from_device).pack(side="left",
                                                            padx=(4, 0))
        self.cal_device_label = ttk.Label(store_row, text="",
                                          style="Dim.TLabel")
        self.cal_device_label.pack(side="right")

        self.cal_text = tk.Text(parent, height=8, font=(MONO, 9),
                                bg=PANEL, fg=FG, wrap="none")
        self.cal_text.pack(fill="both", expand=True, pady=(6, 0))
        self._refresh_cal()

    # -------------------------------------------------------- connection

    def _refresh_ports(self):
        """Rebuild the Port submenu from the ports present right now.

        Called from the Device menu's postcommand, so the list is read when
        the menu is opened rather than when the window was built. A device
        plugged in after startup therefore needs no Rescan.
        """
        self._update_menu_state()
        devices = find_devices()
        self._ports = [d for d, _ in devices]
        menu = getattr(self, "port_menu", None)
        if menu is None:
            return
        menu.delete(0, "end")
        if not devices:
            menu.add_command(label="no serial ports found", state="disabled")
            return
        for dev, desc in devices:
            menu.add_radiobutton(label=f"{dev}  {desc}"[:48], value=dev,
                                 variable=self.port_var)
        if not self.port_var.get():
            self.port_var.set(devices[0][0])

    def _selected_port(self):
        """The chosen port.

        The variable holds the bare device name. A settings file written by
        an older version holds the combined "COM4  description" form, so the
        first field is taken rather than the whole string.
        """
        sel = self.port_var.get()
        if not sel:
            return None
        return sel.split()[0]

    def _toggle_connection(self):
        if self.device.connected:
            self.device.disconnect()
            self._update_menu_state()
            self.dev_label.config(text="no device")
            self.capture_status = None
            self._stack_idle()
            self.sd_fetch = None
            self.sd_fetch_action = None
            self.sd_listing = False
            self.sd_status = None
            self.sd_dirty = True
            self._sd_update_label()
            self._refresh_cal_device_label()
            self._log("disconnected")
            return

        port = self._selected_port()
        if not port:
            messagebox.showwarning(APP_NAME, "No serial port selected.")
            return
        try:
            info = self.device.connect(port)
        except DeviceError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return

        self._update_menu_state()
        self.pixels = info.pixels
        self.accum = processing.Accumulator(self.pixels,
                                            int(self.avg_var.get()))
        # A different device may have a different pixel count, so a zoom
        # expressed in pixel indices from the last one is not meaningful,
        # and neither is a pixel's value history.
        self._reset_zoom()
        self._clear_pixel_history()
        if info.frame_us:
            self._set_period_fields(info.frame_us)
        self._update_device_label()
        self._update_stack_plan()
        # A previous session may have left the device quiet after collecting
        # a result. Frames are what this window is for, so ask for them.
        if info.supports_stacking:
            try:
                self.device.set_live(True)
                self.live_on = True
            except DeviceError:
                pass
        self._update_live_btn()
        # The device has no clock of its own. Without this every file it
        # writes is dated 2000-01-01, on the card and in this window both.
        if info.supports_storage:
            try:
                self.device.sd_set_time()
                self.device.sd_status()
            except DeviceError:
                pass
            self.sd_dirty = True
        # The calibration belongs to the instrument, so the one it holds wins
        # over whatever this computer had left over from the last device it
        # was pointed at. Asked for on every connect; the reply is handled in
        # _on_cal_event, which leaves the local points alone if the device is
        # empty.
        if info.supports_calibration:
            self.cal_pull_pending = "auto"
            try:
                self.device.cal_get()
            except DeviceError:
                self.cal_pull_pending = None
        # A failed boot self test means the SPI link to the ADC is suspect,
        # and a suspect link still produces plausible spectra. Said once, on
        # connect, rather than left for whoever thinks to look at the label.
        if info.self_test_passed is False:
            self._log("[!] the device failed its boot self test; the SPI link "
                      "to the ADC is suspect. Send 'selftest' for the "
                      "detail.")
        self.settings.set("port", port)
        self.settings.save()
        self._log(f"connected to {port}")

    def _update_device_label(self):
        self._update_menu_state()
        self._refresh_cal_device_label()
        i = self.device.info
        if i.firmware == "unknown":
            self.dev_label.config(text="connected, device did not identify")
            return
        # Step size is the smallest distinguishable change: one code.
        # Reported in microvolts because at 14 bits over 2.5 V it is well
        # under a millivolt, and it is the floor against which any noise
        # figure should be read.
        lsb_uv = (i.vref_mv * 1000.0) / i.full_scale
        # The self test runs at boot and prints its lines then. The window is
        # normally opened long afterwards, so the outcome is read from the
        # "I " line and shown here. Older firmware sends no field, which is
        # unknown rather than failed and is left off the label entirely.
        passed = i.self_test_passed
        selftest = ("" if passed is None
                    else ("\nboot self test PASS" if passed
                          else "\nboot self test FAILED, run 'selftest'"))
        self.dev_label.config(
            text=(f"{i.board}  fw {i.firmware}\n"
                  f"{i.pixels} px  {i.bits}-bit  {i.vref_mv} mV full scale\n"
                  f"step {lsb_uv:.2f} uV/code ({i.full_scale} codes)\n"
                  f"sysclk {i.sysclk_mhz} MHz  built {i.built}\n"
                  f"pio {i.pio_crc}{selftest}"))

    def _sync_rate_fields(self):
        """Compatibility hook for callers that set rate_mode directly."""
        self._update_exposure_label()

    def _set_period_fields(self, us):
        if us <= 0:
            return
        self.exposure_var.set(f"{us / 1000.0:.4g}")
        self.fps_var.set(f"{1e6 / us:.4g}")

    def _fps_changed(self, event=None):
        self.rate_mode.set("hz")
        try:
            fps = float(self.fps_var.get())
        except ValueError:
            return
        if fps > 0:
            self.exposure_var.set(f"{1000.0 / fps:.4g}")
            self._update_stack_plan()

    def _integration_changed(self, event=None):
        self.rate_mode.set("ms")
        try:
            ms = float(self.exposure_var.get())
        except ValueError:
            return
        if ms > 0:
            self.fps_var.set(f"{1000.0 / ms:.4g}")
            self._update_stack_plan()

    def _current_us(self):
        try:
            if self.rate_mode.get() == "hz":
                value = float(self.fps_var.get())
                return 1000000.0 / value if value > 0 else None
            value = float(self.exposure_var.get())
        except ValueError:
            return None
        if value <= 0:
            return None
        return value * 1000.0

    def _update_exposure_label(self):
        us = self._current_us()
        if us is None:
            return
        self._set_period_fields(us)
        self._update_stack_plan()

    def _apply_exposure(self, event=None):
        if not self.device.connected:
            return
        if self.stack_state is not None:
            # The device rejects this while capturing anyway, since a capture
            # owns the frame period until it finishes. Saying so here is
            # clearer than an error line in the console.
            messagebox.showwarning(
                APP_NAME, "A stacked capture is running. Stop it first.")
            return
        us = self._current_us()
        if us is None:
            return
        lo = self.device.info.frame_us_min or 20600
        hi = self.device.info.frame_us_max or 10000000
        if us < lo:
            messagebox.showwarning(
                APP_NAME,
                f"Integration time cannot be shorter than the scan.\n\n"
                f"Requested {us/1000:.2f} ms, minimum {lo/1000:.2f} ms "
                f"({1e6/lo:.1f} Hz).")
            us = lo
        us = max(lo, min(hi, us))
        try:
            self.device.set_integration_us(us)
        except DeviceError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        self._update_exposure_label()
        # Exposure sets the noise regime (readout noise, in this
        # instrument's case, does not scale with it, but signal level
        # does), so samples collected under the old setting do not belong
        # in the same distribution as what comes next.
        self._clear_pixel_history()
        if us < 25000:
            self._log(f"[!] {us/1000:.2f} ms is below the 25.00 ms "
                      f"(40 Hz) limit for complete frames; pixels may be "
                      f"reported missing")

    def _apply_average(self):
        try:
            n = int(self.avg_var.get())
        except ValueError:
            n = 1
        self.accum.resize(n)

    # ------------------------------------------------------------ capture

    def _start_capture(self, kind="spectrum"):
        if not self.device.connected:
            messagebox.showwarning(APP_NAME, "Connect to a device first.")
            return
        if self.stack_state is not None:
            messagebox.showwarning(
                APP_NAME,
                "A stacked capture is running on the device, and no frames "
                "are sent while it does. Stop it first.")
            return
        if kind == "spectrum":
            try:
                n = int(self.capture_var.get())
            except ValueError:
                return
        else:
            n = 200
        self.capture_target = max(1, n)
        self.capture_frames = []
        self.capture_kind = kind
        self.capture_started = time.time()
        self._log(f"capture started: {kind}, {self.capture_target} frames")

    def _finish_capture(self):
        kind = self.capture_kind
        frames = self.capture_frames
        elapsed = time.time() - self.capture_started
        self.capture_target = 0
        self.capture_kind = None
        self.capture_frames = []

        if not frames:
            return

        method = "median" if kind in ("dark", "flat") else "mean"
        combined = processing.sigma_clip_combine(frames, method=method)

        if kind == "dark":
            self.dark = combined
            self.use_dark.set(True)
            self._log(f"dark acquired from {len(frames)} frames")
        elif kind == "flat":
            # The flat must be dark-corrected before normalising, or the
            # dark offset is baked into the gain map as if it were response.
            src = processing.apply_dark(combined, self.dark)
            self.flat = processing.normalise_flat(src)
            if self.flat is None:
                messagebox.showwarning(
                    APP_NAME,
                    "Flat field rejected: the exposure has no usable signal. "
                    "Illuminate the detector and retry.")
                self._log("flat rejected: insufficient signal")
            else:
                self.use_flat.set(True)
                self._log(f"flat acquired from {len(frames)} frames")
        else:
            self.frozen = combined
            self._log(f"captured {len(frames)} frames in {elapsed:.1f} s")

        self._update_ref_label()
        self.capture_label.config(text="")

    # ------------------------------------------------------ device stack

    def _stack_plan(self):
        """Frames and total time the current settings would produce.

        Returns (frames, exposure_us, note) with note describing whatever is
        wrong, or None when the settings are usable."""
        us = self._current_us()
        if us is None:
            return 0, None, "set an integration time"
        try:
            secs = float(self.stack_secs_var.get())
        except ValueError:
            return 0, us, "total time is not a number"
        if secs <= 0:
            return 0, us, "total time must be above zero"

        info = self.device.info
        lo = info.frame_us_min or 20600
        hi = info.frame_us_max or 10000000
        if us < lo:
            return 0, us, (f"exposure below the {lo / 1000.0:.2f} ms scan")
        if us > hi:
            return 0, us, (f"exposure above the {hi / 1000.0:.0f} ms limit")

        frames = int(round((secs * 1e6) / us))
        max_frames = info.cap_max_frames or 16384
        max_s = info.cap_max_s or 600
        if frames < 2:
            return frames, us, "fewer than two frames: nothing to reject with"
        if secs > max_s:
            return frames, us, f"longer than the {max_s} s device limit"
        if frames > max_frames:
            return frames, us, f"more than the {max_frames} frame device limit"
        return frames, us, None

    def _update_stack_plan(self):
        pass

    def _start_stack(self):
        if not self.device.connected:
            messagebox.showwarning(APP_NAME, "Connect to a device first.")
            return
        if not self.device.info.supports_stacking:
            messagebox.showwarning(
                APP_NAME,
                "This firmware does not stack on the device.\n\n"
                "Update it, or use Stack in Preview, which "
                "combines frames on this computer instead.")
            return
        if self.stack_state is not None:
            return
        if self.capture_target:
            messagebox.showwarning(
                APP_NAME, "A host-side capture is already running.")
            return

        frames, us, note = self._stack_plan()
        if note is not None:
            messagebox.showwarning(APP_NAME, f"Cannot start: {note}.")
            return

        try:
            sigma = float(self.stack_sigma_var.get())
            block = int(self.stack_block_var.get())
        except ValueError:
            messagebox.showwarning(APP_NAME, "Sigma or block is not a number.")
            return

        kind = self.stack_kind_var.get()
        secs = float(self.stack_secs_var.get())
        # The tag becomes the prefix of the file name on the card, so a
        # listing says what each capture was for without opening it.
        tag = {"dark": "DK", "flat": "FL"}.get(kind, "SP")
        try:
            self.device.capture_start(secs, us, sigma=sigma, block=block,
                                      dark=(kind == "dark"), tag=tag)
        except DeviceError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return

        self.stack_state = "running"
        self.stack_kind = kind
        self.stack_started = time.time()
        self.stack_expected_s = frames * us / 1e6
        self.stack_deadline = (self.stack_started + self.stack_expected_s
                               + STACK_RUN_GRACE_S)
        self.stack_fetch_tries = 0
        self.stack_poll_countdown = STACK_POLL_TICKS
        self.capture_status = None
        self.stack_start_btn.config(state="disabled")
        self.stack_stop_btn.config(state="normal")
        self._log(f"stack started: {frames} frames of {us / 1000.0:.2f} ms "
                  f"for {kind}, {sigma:.2f} sigma, block {block}")
        # The live view stops here. The device transmits nothing at all
        # during a capture, which is the point of running it there.
        self._update_stack_status()

    def _stop_stack(self):
        if self.stack_state == "fetching":
            # Give up on this transfer only. The result stays on the device
            # until the next capture overwrites it.
            self._log("collection abandoned; the result is still on the "
                      "device, send 'cap get' to try again")
            self._stack_idle()
            try:
                # Restore what the Stop live button asked for, not an
                # unconditional stream. A capture must not undo it.
                self.device.set_live(self.live_on)
            except DeviceError:
                pass
            return
        if self.stack_state != "running":
            return
        try:
            self.device.capture_stop()
        except DeviceError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        self._log("stack stop requested; frames already collected are kept")

    def _on_capture_status(self, status):
        self.capture_status = status
        if self.stack_state == "running" and status.finished:
            if status.have_result:
                self.stack_state = "fetching"
                self.stack_fetch_tries = 0
                self._request_result()
            else:
                self._stack_failed("the device stacked no usable frames")
        self._update_stack_status()

    def _request_result(self):
        """Ask for the record and set a deadline for it to arrive."""
        self.stack_fetch_tries += 1
        self.stack_deadline = time.time() + STACK_FETCH_TIMEOUT_S
        try:
            self.device.request_capture_result()
        except DeviceError as exc:
            self._stack_failed(str(exc))

    def _on_sd_error(self, line):
        """A card error that concerns a transfer this window is waiting on.

        Without this a refused request left the browser waiting for a record
        that was never going to be sent, exactly as an unhandled capture
        error once did."""
        self.sd_listing = False
        if self.sd_fetch is not None:
            self._sd_fetch_failed(line.strip())

    def _on_capture_error(self, line):
        """A device error line that concerns a capture we are waiting on.

        Without this a refused request left the window waiting for a record
        that was never going to be sent."""
        if self.stack_state is None:
            return
        self._stack_failed(f"device refused: {line.strip()}")

    def _tick_stack_poll(self):
        """Progress polling and the deadlines, once per poll tick."""
        now = time.time()

        if self.stack_state == "fetching" and now > self.stack_deadline:
            if self.stack_fetch_tries < STACK_FETCH_TRIES:
                self._log(f"[!] no result after {STACK_FETCH_TIMEOUT_S:.0f} s; "
                          f"asking again "
                          f"({self.stack_fetch_tries + 1} of "
                          f"{STACK_FETCH_TRIES})")
                self._request_result()
            else:
                self._stack_failed(
                    f"the result did not arrive after {STACK_FETCH_TRIES} "
                    f"requests. It is still on the device: press Start "
                    f"again, or send 'cap get' from the Console.")
            return

        if self.stack_state == "running" and now > self.stack_deadline:
            self._stack_failed(
                "the device stopped reporting progress. Nothing was "
                "received for "
                f"{STACK_RUN_GRACE_S:.0f} s past the expected end.")
            return

        self.stack_poll_countdown -= 1
        if self.stack_poll_countdown > 0:
            return
        self.stack_poll_countdown = STACK_POLL_TICKS
        if self.stack_state != "running":
            return
        try:
            self.device.request_capture_status()
        except DeviceError:
            pass

    def _stack_failed(self, reason):
        self._log(f"[!] stack failed: {reason}")
        self._stack_idle()
        # Whatever went wrong, the window is useless without frames, and the
        # device is left quiet after a capture until it is told otherwise.
        # Restore what the Stop live button asked for, not an unconditional
        # stream.
        try:
            self.device.set_live(self.live_on)
        except DeviceError:
            pass
        messagebox.showwarning(APP_NAME, f"Capture failed: {reason}")

    def _stack_idle(self):
        self.stack_state = None
        self.stack_kind = None
        self.stack_fetch_tries = 0
        self.stack_start_btn.config(state="normal")
        self.stack_stop_btn.config(state="disabled")
        self._update_stack_status()

    def _finish_stack(self, res):
        """Turn a returned record into a spectrum, a dark or a flat.

        The device returns mean codes. Everything after that is the same
        pipeline a live frame goes through, and in the same order, so a
        stacked spectrum and an averaged live view are directly comparable.

        The notch is applied here rather than on the device on purpose. It is
        a linear operation, so notching the mean gives nearly the same answer
        as averaging notched frames, and keeping a Fourier transform out of
        the capture loop keeps the capture loop simple.
        """
        kind = self.stack_kind or "spectrum"
        info = self.device.info

        codes = np.array(res.mean, dtype=np.float64)
        # A pixel with no surviving sample is missing, not zero. The device
        # already sends NaN for those; this covers a record that reported a
        # value with a zero count.
        codes[res.accepted <= 0] = np.nan
        v = processing.codes_to_volts(codes, info.vref_mv, info.full_scale)
        v = processing.notch_pattern(v)
        if self.mask_ends.get():
            v = processing.mask_end_pixels(v)

        if kind == "dark":
            self.dark = v
            self.use_dark.set(True)
            self.stack_display = None
            self._log(f"dark acquired from {res.frames_stacked} stacked frames")
        elif kind == "flat":
            # Dark-correct before normalising, or the dark offset is baked
            # into the gain map as if it were response.
            self.flat = processing.normalise_flat(
                processing.apply_dark(v, self.dark))
            self.stack_display = None
            if self.flat is None:
                messagebox.showwarning(
                    APP_NAME,
                    "Flat field rejected: the exposure has no usable signal. "
                    "Illuminate the detector and retry.")
                self._log("flat rejected: insufficient signal")
            else:
                self.use_flat.set(True)
                self._log(f"flat acquired from {res.frames_stacked} "
                          f"stacked frames")
        else:
            if self.use_dark.get():
                v = processing.apply_dark(v, self.dark)
            if self.use_flat.get():
                v = processing.apply_flat(v, self.flat)
            self.frozen = v
            self.stack_display = res

        self._log("stack: " + res.summary())
        self._log_stack_quality(res)
        self._update_ref_label()
        self._stack_idle()

        # Frames stopped when the capture started and the device stayed quiet
        # so the result could be collected without sharing the link. Ask for
        # them back, unless the Stop live button says otherwise.
        try:
            self.device.set_live(self.live_on)
        except DeviceError:
            pass
        self._redraw()

    def _log_stack_quality(self, res):
        """Report what the frames behind the result looked like.

        Nothing is transmitted during a capture, so these counters are the
        only evidence the frames were sound. Reported every time rather than
        only on a fault, because a number that appears only when something is
        wrong gives no baseline to judge it against.
        """
        accepted = int(res.accepted.sum())
        rejected = int(res.rejected.sum())
        offered = accepted + rejected
        pct = (100.0 * rejected / offered) if offered else 0.0
        dead = int(np.count_nonzero(res.accepted <= 0))
        sd = res.sd[np.isfinite(res.sd) & (res.accepted > 1)]
        info = self.device.info
        sd_mv = (float(np.median(sd)) * info.vref_mv / info.full_scale
                 if sd.size else float("nan"))

        self._log(f"      clipped {rejected} of {offered} samples "
                  f"({pct:.3f} %), {dead} pixels with no data")
        self._log(f"      frame-to-frame spread {sd_mv:.3f} mV median "
                  f"(raw, mostly the 17.4 kHz pattern, not the noise floor)")
        self._log(f"      device counters: missing {res.stat_missing}  "
                  f"stale {res.stat_stale}  badbits {res.stat_badbits}  "
                  f"worst late {res.stat_max_late_us} us  "
                  f"nostart {res.stat_nostart}")
        if res.frames_excluded:
            self._log(f"[!] {res.frames_excluded} frames were dropped whole; "
                      f"the scan failed on those")
        if dead:
            self._log(f"[!] {dead} pixels received no usable sample in the "
                      f"whole capture")

    def _update_stack_status(self):
        if not hasattr(self, "stack_label"):
            return
        st = self.capture_status
        if self.stack_state == "running":
            if st is None:
                text = "starting..."
            else:
                elapsed = st.elapsed_ms / 1000.0
                total = max(1, st.requested)
                rate = (st.attempted / elapsed) if elapsed > 0 else 0.0
                left = ((total - st.attempted) / rate) if rate > 0 else 0.0
                text = (f"{st.attempted}/{total} frames  {elapsed:5.1f} s  "
                        f"{left:5.1f} s left\nexcluded {st.excluded}")
            self.stack_label.config(text=text, foreground=FG)
        elif self.stack_state == "fetching":
            tries = ("" if self.stack_fetch_tries <= 1
                     else f" (attempt {self.stack_fetch_tries})")
            self.stack_label.config(text=f"collecting result...{tries}",
                                    foreground=FG)
        elif st is not None and st.finished:
            self.stack_label.config(
                text=(f"last: {st.stacked} frames in "
                      f"{st.duration_ms / 1000.0:.1f} s"), foreground=FG_DIM)
        else:
            self.stack_label.config(text="idle", foreground=FG_DIM)

    def _showing_live(self):
        """Whether the plot is showing live data right now.

        Two separate things stop it: the device not transmitting, and a held
        spectrum covering the display. The button label follows THIS, not
        self.live_on alone. Loading a stored spectrum leaves the device
        transmitting, so a button reading self.live_on still said "Pause"
        over a frozen trace that nothing was updating.
        """
        return (self.live_on and self.frozen is None
                and self.stack_display is None)

    def _toggle_live(self):
        """Start or stop the live view.

        This stops the device transmitting. It keeps scanning either way, so
        the detector is never left integrating with nothing to flush it.

        The button changes only if the device accepted the command. A button
        that flipped on a failed write would report a state the device is not
        in.
        """
        want = not self._showing_live()
        # Already transmitting, with a held spectrum on top: there is nothing
        # to ask the device for, only something to put away.
        if want and self.live_on:
            self.frozen = None
            self.stack_display = None
            self._update_live_btn()
            self._redraw()
            return
        try:
            self.device.set_live(want)
        except DeviceError as exc:
            self._log(f"live: {exc}")
            return
        self.live_on = want
        if want:
            # A stacked result or a loaded file may be held on screen. Asking
            # for the live view is asking to see live data again.
            self.frozen = None
            self.stack_display = None
        self._update_live_btn()
        self._redraw()

    def _update_live_btn(self):
        # Called from _redraw(), which a theme switch can reach before the
        # panels exist.
        if not hasattr(self, "live_btn"):
            return
        self.live_btn.config(text="Pause" if self._showing_live() else "Play")

    # --------------------------------------------------------- references

    def _load_ref(self, kind):
        path = filedialog.askopenfilename(
            title=f"Load {kind} reference", filetypes=[("NumPy", "*.npy")])
        if not path:
            return
        try:
            data = export.read_reference(path, self.pixels)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not load {kind}: {exc}")
            return
        if kind == "dark":
            self.dark = data
            self.use_dark.set(True)
        else:
            self.flat = data
            self.use_flat.set(True)
        self._update_ref_label()
        self._log(f"{kind} loaded from {Path(path).name}")

    def _update_ref_label(self):
        def describe(a, unit):
            if a is None:
                return "none"
            finite = a[np.isfinite(a)]
            if finite.size == 0:
                return "empty"
            return f"mean {np.mean(finite):.4f} {unit}"
        self.ref_label.config(
            text=(f"dark: {describe(self.dark, 'V')}\n"
                  f"flat: {describe(self.flat, 'x')}"))

    # ------------------------------------------------------------- render

    def _process(self, frame):
        i = self.device.info
        v = processing.codes_to_volts(frame.codes, i.vref_mv, i.full_scale)
        # notch_pattern guards the end pixels itself, so this is
        # independent of the mask setting and of the order of the two.
        v = processing.notch_pattern(v)
        if self.mask_ends.get():
            v = processing.mask_end_pixels(v)
        if self.use_dark.get():
            v = processing.apply_dark(v, self.dark)
        if self.use_flat.get():
            v = processing.apply_flat(v, self.flat)
        return v

    def _poll(self):
        try:
            self._poll_once()
        finally:
            self.root.after(POLL_MS, self._poll)

    def _poll_once(self):
        for kind, text in self.device.drain_messages():
            if kind == "info":
                self._update_device_label()
            elif kind == "error" and text.startswith("E cap"):
                self._on_capture_error(text)
            elif kind == "error" and text.startswith("E sd"):
                self._on_sd_error(text)
            self._log(text, tag=self._log_tag_for(kind, text))

        # Before frames, because a capture produces no frames and the early
        # return below would otherwise skip the whole capture state machine
        # for as long as one runs.
        for status in self.device.drain_capture_events():
            self._on_capture_status(status)
        for kind, payload in self.device.drain_sd_events():
            self._on_sd_event(kind, payload)
        for info in self.device.drain_cal_events():
            self._on_cal_event(info)
        for result in self.device.drain_stacks():
            # One record, two possible destinations. sd_fetch is set only
            # while a stored file has been asked for, and the device refuses
            # to send a second record while one is in flight, so this cannot
            # be ambiguous.
            if self.sd_fetch is not None:
                self._finish_sd_fetch(result)
            else:
                self._finish_stack(result)
        if self.sd_fetch is not None and time.time() > self.sd_fetch_deadline:
            self._sd_fetch_failed(
                f"nothing arrived within {SD_FETCH_TIMEOUT_S:.0f} s")
        if self.stack_state is not None:
            self._tick_stack_poll()

        stats = self.device.drain_stats()
        if stats:
            self.last_stats = stats[-1]
            if self.show_stats.get():
                for st in stats[-6:]:
                    self._log(st.text, tag="stat")
            self._update_diag_summary()

        frames = self.device.drain_frames()
        if not frames:
            self._update_status()
            return

        self._fps_n += len(frames)
        now = time.time()
        if now - self._fps_t >= 1.0:
            self.fps = self._fps_n / (now - self._fps_t)
            self._fps_n = 0
            self._fps_t = now

        for f in frames:
            v = self._process(f)
            self.live_volts = v
            self.accum.push(v)
            self.pixel_history.append(v)
            self.pixel_history_raw.append(processing.codes_to_volts(
                f.codes, self.device.info.vref_mv, self.device.info.full_scale))
            if self.capture_target:
                self.capture_frames.append(v)
                if len(self.capture_frames) >= self.capture_target:
                    self._finish_capture()
                    break
                self.capture_label.config(
                    text=f"capturing {len(self.capture_frames)}"
                         f"/{self.capture_target}")

        self.display = self.accum.mean()
        self._redraw()
        self._update_status()

    def _current_data(self):
        return self.frozen if self.frozen is not None else self.display

    def _redraw(self):
        # Here rather than at each of the seven places that set or clear a
        # held spectrum. Everything that changes what is on screen ends in a
        # redraw, so the button cannot fall out of step with the plot.
        self._update_live_btn()
        data = self._current_data()
        if data is None:
            return
        self._draw_strip(data)
        self._draw_plot(data)
        self._update_stats(data)
        # Per-frame doesn't need a clicked pixel; the others do.
        if self.hist_px is not None or self.hist_view.get() == "spectrum_space":
            self._update_pixel_hist()

    def _draw_strip(self, volts):
        i = self.device.info
        # Absolute shading: black is 0 V and white is the reference
        # voltage. The scale never follows the data, so the brightness
        # of a pixel means the same thing in every frame and between
        # one measurement and the next.
        full = i.vref_mv / 1000.0
        cells = []
        for v in volts:
            if not np.isfinite(v):
                cells.append(MISSING)
            else:
                g = int(max(0.0, min(1.0, v / full)) * 255)
                cells.append(f"#{g:02x}{g:02x}{g:02x}")
        row = "{" + " ".join(cells) + "}"
        self.strip_img.put(" ".join([row] * STRIP_H))

        # The strip always shows the whole frame; it is the overview a zoom
        # is taken from. When the plot is zoomed, outline the corresponding
        # span here so the zoomed region stays visible in context instead
        # of being cropped out of sight.
        self.strip.delete("zoombox")
        if self.view_x is not None:
            x_lo, x_hi = self.view_x
            n = len(volts)
            x0 = MARGIN_L + max(0.0, x_lo) / n * PLOT_W
            x1 = MARGIN_L + min(float(n), x_hi) / n * PLOT_W
            self.strip.create_rectangle(
                x0, 0, x1, STRIP_H - 1, outline=TRACE_REF, width=2,
                tags="zoombox")

    def _home_x(self, volts):
        return 0.0, float(len(volts))

    def _home_y(self):
        """Fixed full-scale axis, 0 V to the reference voltage.

        Deliberately not autoscaled by default. An axis that rescales to
        the data makes noise on a weak signal look identical to structure
        on a strong one, and hides how much of the converter range is in
        use. A fixed home view keeps successive measurements directly
        comparable and makes approaching saturation obvious. Zooming in on
        a region is a separate, explicit action the operator takes, not an
        automatic rescale, and Home always returns to this fixed range.

        Dark subtraction can push corrected values slightly below zero,
        which is normal, so a small negative margin keeps those visible
        rather than clipped against the axis.
        """
        full = self.device.info.vref_mv / 1000.0
        return -0.02 * full, full

    def _view(self, volts):
        """Current (x_lo, x_hi, y_lo, y_hi) in data units: pixel index and
        volts. Falls back to the home range on either axis not set."""
        x_lo, x_hi = self.view_x if self.view_x is not None else self._home_x(volts)
        y_lo, y_hi = self.view_y if self.view_y is not None else self._home_y()
        return x_lo, x_hi, y_lo, y_hi

    def _tick_step_mv(self, lo, hi):
        """Round tick step giving roughly 8 to 12 divisions over the span."""
        span_mv = (hi - lo) * 1000.0
        for step in (1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 2500):
            if span_mv / step <= 12:
                return step
        return 5000

    def _tick_step_px(self, span_px):
        """Round tick step in pixels, giving roughly 6 to 12 divisions.
        Needed once the view can be narrower than the fixed 128-pixel
        spacing used at the home zoom, where that spacing gave 8 ticks
        across the full 1024-pixel width."""
        for step in (1, 2, 5, 10, 20, 25, 50, 100, 128, 200, 256, 512):
            if span_px / step <= 12:
                return step
        return 1024

    def _draw_plot(self, volts):
        c = self.plot
        c.delete("all")
        n = len(volts)
        x_lo, x_hi, lo, hi = self._view(volts)
        if x_hi <= x_lo or hi <= lo:
            # A degenerate view (zero or negative span) cannot be drawn;
            # fall back to home rather than dividing by zero.
            self.view_x = self.view_y = None
            x_lo, x_hi, lo, hi = self._view(volts)
        h = PLOT_H - MARGIN_T - MARGIN_B

        def y_of(v):
            return MARGIN_T + h * (1.0 - (v - lo) / (hi - lo))

        def x_of(px):
            return MARGIN_L + (px - x_lo) / (x_hi - x_lo) * PLOT_W

        # Ticks on round values, not on fractions of the range. Dividing the
        # span into equal parts gives labels like 587.5 and 1862.5, which
        # are hard to read a value against; a fixed step in millivolts
        # gives round numbers and a constant scale between measurements.
        step_mv = self._tick_step_mv(lo, hi)
        first = int(np.ceil(lo * 1000 / step_mv))
        last = int(np.floor(hi * 1000 / step_mv))
        for k in range(first, last + 1):
            mv = k * step_mv
            y = y_of(mv / 1000.0)
            c.create_line(MARGIN_L, y, MARGIN_L + PLOT_W, y,
                          fill=GRID_MAJOR if mv == 0 else GRID)
            c.create_text(MARGIN_L - 6, y, text=f"{mv:g}",
                          fill=AXIS, anchor="e", font=(MONO, 8))
        c.create_text(MARGIN_L - 6, MARGIN_T - 2, text="mV", fill=AXIS,
                      anchor="se", font=(MONO, 8))

        wl = self.cal.axis(n)
        # Tick spacing adapts to how much of the pixel axis is in view, so
        # a small zoomed region still gets several readable ticks instead
        # of the single fixed-128 spacing used at the home view.
        step_px = self._tick_step_px(x_hi - x_lo)
        first_px = int(np.floor(x_lo / step_px)) * step_px
        for px in range(max(0, first_px), min(n, int(np.ceil(x_hi)) + 1),
                        step_px):
            x = x_of(px)
            if x < MARGIN_L - 1 or x > MARGIN_L + PLOT_W + 1:
                continue
            c.create_line(x, MARGIN_T, x, MARGIN_T + h, fill=GRID)
            label = f"{wl[px]:.1f}" if wl is not None else str(px)
            c.create_text(x, MARGIN_T + h + 21, text=label, fill=AXIS,
                          font=(MONO, 8))
        # Unit centred beneath the plot rather than at the right edge,
        # where it previously overlapped the final tick label.
        c.create_text(MARGIN_L + PLOT_W // 2, MARGIN_T + h + 32,
                      text="wavelength (nm)" if wl is not None else "pixel",
                      fill=AXIS, font=(MONO, 8))

        # Break the trace at missing pixels rather than interpolating over
        # them: a straight line across a gap is invented data. Only the
        # pixels within (or bordering) the current view are walked, since a
        # deep zoom into a handful of pixels has no need to touch the rest
        # of the 1024-pixel array every redraw. The one pixel bordering
        # each side of the view is included so the trace connects smoothly
        # to what is just off screen; those connecting segments (and any
        # segment whose data value falls outside a narrow y-zoom) are then
        # clipped to the plot rectangle rather than drawn straight through
        # it, since Tkinter has no notion of the axis box and would
        # otherwise let the trace run out past the grid.
        lo_i = max(0, int(np.floor(x_lo)) - 1)
        hi_i = min(n, int(np.ceil(x_hi)) + 1)
        rect = self._plot_rect()

        def inside(pt):
            return rect[0] <= pt[0] <= rect[2] and rect[1] <= pt[1] <= rect[3]

        def flush(run):
            if len(run) < 2:
                return
            if all(inside(p) for p in run):
                c.create_line(*(v for p in run for v in p),
                              fill=TRACE, width=1)
                return
            for a, b in zip(run, run[1:]):
                seg = self._clip_segment((*a, *b), rect)
                if seg is not None:
                    c.create_line(*seg, fill=TRACE, width=1)

        run = []
        for idx in range(lo_i, hi_i):
            v = volts[idx]
            if not np.isfinite(v):
                flush(run)
                run = []
                # Only mark pixels actually in view; the border pixel
                # exists purely to anchor the connecting line above and
                # has no in-view slot of its own to mark.
                if x_lo <= idx <= x_hi:
                    x = x_of(idx)
                    c.create_line(x, MARGIN_T, x, MARGIN_T + h, fill=MISSING)
            else:
                run.append((x_of(idx), y_of(v)))
        flush(run)

        self._find_peaks(volts)
        for pk in self.peaks:
            if pk["centre"] < x_lo or pk["centre"] > x_hi:
                continue
            x = x_of(pk["centre"])
            y = y_of(pk["height"])
            c.create_line(x, y - 12, x, y - 4, fill=PEAK)
            label = (f"{wl[int(pk['index'])]:.1f}" if wl is not None
                     else f"{pk['centre']:.0f}")
            c.create_text(x, y - 18, text=label, fill=PEAK,
                          font=(MONO, 8))

        if self.cursor_px is not None and x_lo <= self.cursor_px <= x_hi:
            x = x_of(self.cursor_px)
            c.create_line(x, MARGIN_T, x, MARGIN_T + h, fill=CURSOR, dash=(2, 3))

        for px, _wlv in self.cal.points:
            if px < x_lo or px > x_hi:
                continue
            x = x_of(px)
            c.create_line(x, MARGIN_T, x, MARGIN_T + 8, fill=TRACE_REF, width=2)

        self._update_zoom_label(volts)

        # c.delete("all") above erased any in-progress selection box along
        # with everything else; put it back on top so a drag stays visible
        # through frames arriving while the mouse button is still down.
        if self._drag_last is not None:
            self._draw_drag_rect()

    def _find_peaks(self, volts):
        try:
            prom = float(self.prom_var.get())
        except (ValueError, AttributeError):
            prom = 0.005
        self.peaks = processing.find_peaks(volts, prom)
        if not hasattr(self, "peak_text"):
            return
        self.peak_text.delete("1.0", "end")
        wl = self.cal.axis(len(volts))
        if not self.peaks:
            self.peak_text.insert("end", "no peaks above threshold\n")
            return
        header = ("  #   pixel      nm      height(mV)  prom(mV)   FWHM(px)\n"
                  if wl is not None else
                  "  #   pixel   height(mV)  prom(mV)   FWHM(px)\n")
        self.peak_text.insert("end", header)
        for n, pk in enumerate(self.peaks, 1):
            if wl is not None:
                nm = float(np.interp(pk["centre"], np.arange(len(wl)), wl))
                self.peak_text.insert(
                    "end",
                    f"{n:3d} {pk['centre']:8.2f} {nm:9.3f} "
                    f"{pk['height']*1000:10.3f} {pk['prominence']*1000:9.3f} "
                    f"{pk['width']:9.1f}\n")
            else:
                self.peak_text.insert(
                    "end",
                    f"{n:3d} {pk['centre']:8.2f} "
                    f"{pk['height']*1000:10.3f} {pk['prominence']*1000:9.3f} "
                    f"{pk['width']:9.1f}\n")

    def _update_stats(self, volts):
        st = processing.statistics(volts)
        if st["valid"] == 0:
            self.stat_label.config(text="no valid pixels")
            return
        lsb_mv = self.device.info.vref_mv / self.device.info.full_scale
        noise = self.accum.std()
        noise_finite = noise[np.isfinite(noise)]
        rms = float(np.mean(noise_finite)) if noise_finite.size else float("nan")
        lines = [
            f"valid    {st['valid']:>6d} / {st['n']}",
            f"min      {st['min']*1000:>9.3f} mV",
            f"max      {st['max']*1000:>9.3f} mV",
            f"mean     {st['mean']*1000:>9.3f} mV",
            f"median   {st['median']*1000:>9.3f} mV",
            f"spread   {st['spatial_std']*1000:>9.3f} mV  across px",
            f"peak-pk  {st['p2p']*1000:>9.3f} mV",
        ]
        # SNR needs repeated measurements of the same pixel, so it is only
        # defined once the accumulator holds more than one frame. The
        # spatial spread above is NOT noise: on a real spectrum it is
        # dominated by spectral structure and the illumination profile.
        if np.isfinite(rms) and self.accum.depth >= 2:
            baseline = 0.0
            if self.use_dark.get() and self.dark is not None:
                baseline = 0.0  # already removed by dark subtraction
            ratio, db = processing.snr_from_noise(st["mean"], rms, baseline)
            if np.isfinite(ratio):
                lines.append(f"SNR      {ratio:>9.1f}  ({db:.1f} dB)")
        if np.isfinite(rms):
            # Expressing frame-to-frame noise in codes as well as volts
            # shows immediately whether it sits at the quantisation floor
            # or well above it, which a millivolt figure alone does not.
            lines.append(f"frame sd {rms*1000:>9.4f} mV "
                         f"({rms*1000/lsb_mv:.1f} codes, n={self.accum.depth})")
        lines.append(f"step     {lsb_mv*1000:>9.2f} uV/code")
        self.stat_label.config(text="\n".join(lines))

    def _update_status(self):
        parts = []
        if self.device.connected:
            parts.append(f"{self.device.port}")
            parts.append(f"{self.fps:5.2f} fps")
            parts.append(f"{self.device.frames_received} frames")
        else:
            parts.append("Disconnected")
        if self.last_stats is not None:
            s = self.last_stats
            parts.append(f"miss {s.miss}")
            parts.append(f"period {s.period_us}us")
            if s.wdog or s.nostart:
                parts.append("FAULT")
        if self.stack_state == "running":
            st = self.capture_status
            done = st.attempted if st is not None else 0
            total = st.requested if st is not None else 0
            parts.append(f"STACKING {done}/{total}")
        elif self.stack_state == "fetching":
            parts.append("COLLECTING RESULT")
        if self.frozen is not None:
            parts.append("STACK" if self.stack_display is not None
                         else "FROZEN")
        if self.accum.depth > 1:
            parts.append(f"avg {self.accum.depth}")
        self.status.config(text="   ".join(parts))

    # -------------------------------------------------------- interaction

    def _plot_rect(self):
        """Canvas bounding box of the plotting area (inside the axes),
        used both to draw against and to clamp mouse coordinates into
        during a drag, so a selection dragged past the edge of the canvas
        still resolves to a sensible boundary rather than an out-of-range
        coordinate."""
        h = PLOT_H - MARGIN_T - MARGIN_B
        return MARGIN_L, MARGIN_T, MARGIN_L + PLOT_W, MARGIN_T + h

    @staticmethod
    def _clip_segment(seg, rect):
        """Liang-Barsky clip of a line segment to a rectangle.

        `seg` is (x0, y0, x1, y1) in canvas coordinates, `rect` is
        (left, top, right, bottom). Returns the visible portion of the
        segment as (x0, y0, x1, y1), or None if it does not touch the
        rectangle at all.

        The trace walks one pixel beyond the current view on each side so
        it can connect smoothly to what is just off screen, and a narrow
        y-zoom can put the data itself above or below the visible range.
        Tkinter draws whatever coordinates it is given with no regard for
        the axis box, so without this the trace visibly runs outside the
        grid whenever either of those happens.
        """
        x0, y0, x1, y1 = seg
        left, top, right, bottom = rect
        dx, dy = x1 - x0, y1 - y0
        t0, t1 = 0.0, 1.0
        for p, q in ((-dx, x0 - left), (dx, right - x0),
                     (-dy, y0 - top), (dy, bottom - y0)):
            if p == 0:
                if q < 0:
                    return None
                continue
            t = q / p
            if p < 0:
                if t > t1:
                    return None
                t0 = max(t0, t)
            else:
                if t < t0:
                    return None
                t1 = min(t1, t)
        if t0 > t1:
            return None
        return (x0 + t0 * dx, y0 + t0 * dy, x0 + t1 * dx, y0 + t1 * dy)

    def _px_from_event(self, event, volts=None):
        """Data pixel index under the cursor, accounting for the current
        zoom. None if the cursor is outside the plot area or there is no
        data to index into."""
        if volts is None:
            volts = self._current_data()
        if volts is None:
            return None
        x0, _y0, x1, _y1 = self._plot_rect()
        if event.x < x0 or event.x > x1:
            return None
        x_lo, x_hi, _y_lo, _y_hi = self._view(volts)
        frac = (event.x - x0) / (x1 - x0)
        idx = int(round(x_lo + frac * (x_hi - x_lo)))
        if idx < 0 or idx >= len(volts):
            return None
        return idx

    def _set_cursor(self, px):
        self.cursor_px = px
        if px is None:
            self.hover_label.config(text="")

    def _on_hover(self, event):
        data = self._current_data()
        px = self._px_from_event(event, data)
        self._set_cursor(px)
        if px is None or data is None:
            return
        v = data[px]
        wl = self.cal.axis(len(data))
        text = f"pixel {px}"
        if wl is not None:
            text += f"   {wl[px]:.3f} nm"
        text += ("   missing" if not np.isfinite(v)
                 else f"   {v*1000:.3f} mV")
        self.hover_label.config(text=text)

    def _on_click(self, event):
        px = self._px_from_event(event)
        if px is not None:
            self.cal_px.set(str(px))
            self._select_pixel(px)

    # ------------------------------------------------------- pixel history

    def _select_pixel(self, px):
        self.hist_px = px
        self._update_pixel_hist()

    def _clear_pixel_history(self):
        self.pixel_history.clear()
        self.pixel_history_raw.clear()
        self.hist_fs = None
        if self.hist_px is not None or self.hist_view.get() == "spectrum_space":
            self._update_pixel_hist()

    def _pixel_series(self, px):
        """Finite values of one pixel across the accumulated history, in
        volts, oldest first. Missing-pixel frames are excluded rather than
        counted as zero, for the same reason NaN is used everywhere else
        in the pipeline: a dropped reading is not a measurement of zero."""
        if not self.pixel_history or px is None:
            return np.array([])
        vals = np.array([frame[px] for frame in self.pixel_history],
                        dtype=np.float64)
        return vals[np.isfinite(vals)]

    def _sample_rate_hz(self):
        """Best estimate of the pixel history's sample rate, in Hz.

        Prefers the measured live frame rate (self.fps, refreshed roughly
        once a second in _poll_once) over the configured one, since actual
        timing can differ slightly from what was requested and the power
        spectrum's frequency axis is only as accurate as this number."""
        if self.fps and self.fps > 0:
            return self.fps
        frame_us = getattr(self.device.info, "frame_us", None)
        if frame_us:
            return 1e6 / frame_us
        return None

    @staticmethod
    def _fmt_hz(f):
        """Compact frequency label: 1, 10, 1k, 17.7k.

        Round gridline frequencies (powers of ten and their 3x
        half-decade) format cleanly either way, but the peak marker sits
        at an arbitrary FFT bin, and plain %g defaults to 6 significant
        figures: a peak at 1.5333333 Hz was rendering as "1.53333"
        rather than "1.53". Capped at 3 significant figures throughout.
        """
        if f >= 1000.0:
            return f"{f / 1000.0:.3g}k"
        return f"{f:.3g}"

    def _update_pixel_hist(self):
        c = self.hist_canvas
        c.delete("all")
        view = self.hist_view.get()

        # Per-frame is a property of the whole frame, not of any one
        # pixel (the interferer it looks for is uniform across the
        # array), so it draws without needing a click.
        if view == "spectrum_space":
            self._draw_spectrum_space(c)
            return

        px = self.hist_px
        if px is None:
            self.hist_label.config(text="No pixel")
            return
        if view == "spectrum_time":
            self._draw_spectrum_time(px, c)
        else:
            self._draw_pixel_histogram(px, c)

    def _draw_pixel_histogram(self, px, c):
        wl = self.cal.axis(self.pixels)
        name = f"pixel {px}"
        if wl is not None:
            name += f"  ({wl[px]:.2f} nm)"

        samples = self._pixel_series(px)
        n = samples.size
        # A handful of samples cannot show a distribution; wait for enough
        # history to be meaningful rather than draw a misleading histogram
        # from 2 or 3 points.
        if n < 8:
            self.hist_label.config(
                text=f"{name}   n={n} sample(s), collecting"
                     f" ({self.pixel_history.maxlen} max)...")
            return

        mv = samples * 1000.0
        mean = float(np.mean(mv))
        std = float(np.std(mv, ddof=1))

        w = c.winfo_width()
        h = c.winfo_height()
        if w <= 1:
            w = HIST_W
        if h <= 1:
            h = HIST_H
        ml, mr = HIST_MARGIN_L, HIST_MARGIN_R
        mt, mb = HIST_MARGIN_T, HIST_MARGIN_B
        plot_w = max(10, w - ml - mr)
        plot_h = max(10, h - mt - mb)

        # Square-root choice of bin count: enough resolution to see the
        # shape without so many bins that a few hundred samples turn into
        # a comb of mostly-empty ones.
        bins = int(np.clip(round(np.sqrt(n)), 8, 40))
        counts, edges = np.histogram(mv, bins=bins)
        x_lo, x_hi = float(edges[0]), float(edges[-1])
        if x_hi <= x_lo:
            x_hi = x_lo + 1.0
        y_max = max(1, int(counts.max()))

        def x_of(v):
            return ml + (v - x_lo) / (x_hi - x_lo) * plot_w

        def y_of(count):
            # Clamped so the Gaussian curve below cannot poke out the top
            # of the panel when its peak is a little taller than the
            # tallest histogram bar, the same edge-overflow this fix
            # avoided on the main plot.
            frac = min(1.0, max(0.0, count / y_max))
            return mt + plot_h * (1.0 - frac)

        step = self._tick_step_mv(x_lo / 1000.0, x_hi / 1000.0)
        first = int(np.ceil(x_lo / step))
        last = int(np.floor(x_hi / step))
        for k in range(first, last + 1):
            v = k * step
            x = x_of(v)
            c.create_line(x, mt, x, mt + plot_h, fill=GRID)
            c.create_text(x, mt + plot_h + 14, text=f"{v:g}", fill=AXIS,
                          anchor="n", font=(MONO, 8))
        c.create_text(ml + plot_w // 2, mt + plot_h + 25, text="mV",
                      fill=AXIS, font=(MONO, 8))

        def band_of(v):
            """Which sigma band a value falls in: 0, 1, 2, or 3 for beyond."""
            if std <= 0:
                return 0
            return min(3, int(abs(v - mean) / std))

        def band_fill(band):
            return (SIGMA_OUTLIER if band >= 3 else SIGMA_COLOURS[band])

        # Band edges, so the shading changes exactly at 1, 2 and 3 sigma
        # rather than at whichever bin edge happens to be nearest. Drawn
        # before the bars, so they stay visible above a short bar in the
        # tails, which is the part worth looking at.
        cuts = []
        if std > 0:
            for k in (1, 2, 3):
                for side in (-1, 1):
                    v = mean + side * k * std
                    if x_lo < v < x_hi:
                        cuts.append(v)
                        c.create_line(x_of(v), mt, x_of(v), mt + plot_h,
                                      fill=GRID_MAJOR, dash=(1, 3))
            cuts.sort()

        for i in range(bins):
            lo_v, hi_v = float(edges[i]), float(edges[i + 1])
            by0 = y_of(counts[i])
            # A bin can straddle a band edge. Split it there instead of
            # colouring the whole bar by its centre, or a wide bin would put
            # a block of "within 1 sigma" colour outside 1 sigma.
            xs = [lo_v] + [v for v in cuts if lo_v < v < hi_v] + [hi_v]
            for a, b in zip(xs, xs[1:]):
                c.create_rectangle(x_of(a), by0, x_of(b), mt + plot_h,
                                   fill=band_fill(band_of(0.5 * (a + b))),
                                   outline="")
            # One outline around the whole bar, so neighbouring bars stay
            # separable the way they were before the shading was added.
            c.create_rectangle(x_of(lo_v), by0, x_of(hi_v), mt + plot_h,
                               fill="", outline=BG)

        # Gaussian curve at the sample mean and standard deviation,
        # scaled to the same count axis as the bars (probability density
        # times sample count times bin width), so it can be compared to
        # the histogram by eye rather than just asserted.
        if std > 0:
            xs = np.linspace(x_lo, x_hi, 120)
            bin_width = edges[1] - edges[0]
            pdf = (np.exp(-0.5 * ((xs - mean) / std) ** 2)
                   / (std * np.sqrt(2.0 * np.pi)))
            ys = pdf * n * bin_width
            pts = []
            for xv, yv in zip(xs, ys):
                pts.append(x_of(xv))
                pts.append(y_of(yv))
            if len(pts) >= 4:
                c.create_line(*pts, fill=PEAK, width=2)

        c.create_line(x_of(mean), mt, x_of(mean), mt + plot_h,
                      fill=CURSOR, dash=(2, 3))
        c.create_line(ml, mt + plot_h, ml + plot_w, mt + plot_h, fill=AXIS)

        lsb_mv = (self.device.info.vref_mv / self.device.info.full_scale
                  if self.device.connected else None)
        text = f"{name}   n={n}   mean={mean:.3f} mV   std={std:.4f} mV"
        if lsb_mv:
            text += f"  ({std/lsb_mv:.1f} codes)"

        # Measured against expected, not measured alone. A band fraction on
        # its own says nothing; the pair says whether this pixel is Gaussian,
        # and therefore whether a sigma clip on it will reject outliers or
        # just eat the tails of a distribution that has no outliers.
        if std > 0:
            k = np.abs(mv - mean) / std
            got = (float(np.mean(k < 1.0)),
                   float(np.mean((k >= 1.0) & (k < 2.0))),
                   float(np.mean((k >= 2.0) & (k < 3.0))),
                   float(np.mean(k >= 3.0)))
            parts = []
            for label, g, e in zip(("1s", "2s", "3s", ">3s"), got,
                                   SIGMA_EXPECTED):
                parts.append(f"{label} {100.0 * g:5.1f}% ({100.0 * e:4.1f})")
            beyond = int(np.count_nonzero(k >= 3.0))
            text += ("\n" + "   ".join(parts)
                     + f"   {beyond} sample(s) a 3 sigma clip would reject")
        self.hist_label.config(text=text)

    def _draw_log_spectrum(self, c, freqs, amps_mv, peak_min_freq=None):
        """Shared renderer for both spectrum views: log10(frequency) on X,
        dB relative to 1 mV on Y.

        Log-log rather than linear, because a spectrum is exactly the
        case linear axes are wrong for: real content spans decades on
        both axes (a peak plus a noise floor two or three orders of
        magnitude below it, over a frequency range that itself may span
        decades), and a linear plot would show a single spike against a
        flat line at zero for everything else.

        `freqs` must be entirely > 0 (DC excluded by the caller; log(0)
        is undefined) and the same length as `amps_mv`, in mV. Returns
        (peak_freq, peak_amp_mv) for the strongest bin at or above
        `peak_min_freq` (the full curve is always drawn regardless), or
        None if there is nothing to draw.

        `peak_min_freq` exists because "strongest bin" and "the thing
        worth flagging" are not always the same bin. Measured live: the
        real illumination profile, seen through a narrow pixel window,
        routinely has more raw amplitude at the low end than the 17.68
        kHz interferer has anywhere, simply because it is real signal and
        large. Marking the global maximum there would point at "the
        array is illuminated," true but not the point of this view.
        Restricting the search rather than restricting what is drawn
        keeps the plot honest while making the marker useful.

        Tick labels are edge-aware: a label whose centre falls near
        either side of the plot area switches from a centred anchor to
        one anchored on the inward side, so it is pushed in from the
        canvas edge rather than drawn half off it. This is the fix for
        labels being cut off at the ends of the axis.
        """
        w = c.winfo_width()
        h = c.winfo_height()
        if w <= 1:
            w = HIST_W
        if h <= 1:
            h = HIST_H
        ml, mr = HIST_MARGIN_L, HIST_MARGIN_R
        mt, mb = HIST_MARGIN_T, HIST_MARGIN_B
        plot_w = max(10, w - ml - mr)
        plot_h = max(10, h - mt - mb)

        c.create_rectangle(ml, mt, ml + plot_w, mt + plot_h, outline=AXIS)
        if freqs.size == 0:
            return None

        log_f = np.log10(freqs)
        x_lo, x_hi = float(log_f.min()), float(log_f.max())
        if x_hi <= x_lo:
            x_hi = x_lo + 1.0

        # Floored before the log so a zero or numerically tiny bin
        # cannot send dB to -inf and break the axis. -120 dB re 1 mV is
        # 1 nV, far below anything this instrument's noise floor is.
        floor_mv = 1e-6
        amps_safe = np.maximum(amps_mv, floor_mv)
        db = 20.0 * np.log10(amps_safe)
        y_lo = -120.0
        y_hi = max(float(db.max()) + 6.0, y_lo + 20.0)

        def x_of(f):
            return ml + (np.log10(f) - x_lo) / (x_hi - x_lo) * plot_w

        def y_of(d):
            d = min(max(d, y_lo), y_hi)
            return mt + plot_h * (1.0 - (d - y_lo) / (y_hi - y_lo))

        # X gridlines at each decade, plus the half-decade (3x) point
        # when there are 3 or fewer decades on screen, so a narrow span
        # (the time-axis view, often under 2 decades) still gets more
        # than one or two gridlines.
        k_lo, k_hi = int(np.floor(x_lo)), int(np.ceil(x_hi))
        x_ticks = set()
        for k in range(k_lo, k_hi + 1):
            x_ticks.add(10.0 ** k)
            if k_hi - k_lo <= 3:
                x_ticks.add(3.0 * (10.0 ** k))
        for f in sorted(x_ticks):
            if not (x_lo - 0.02 <= np.log10(f) <= x_hi + 0.02):
                continue
            x = x_of(f)
            if x < ml - 1 or x > ml + plot_w + 1:
                continue
            c.create_line(x, mt, x, mt + plot_h, fill=GRID)
            if x < ml + 16:
                anchor = "nw"
            elif x > ml + plot_w - 16:
                anchor = "ne"
            else:
                anchor = "n"
            c.create_text(x, mt + plot_h + 14, text=self._fmt_hz(f),
                          fill=AXIS, anchor=anchor, font=(MONO, 8))
        c.create_text(ml + plot_w // 2, mt + plot_h + 25, text="frequency",
                      fill=AXIS, font=(MONO, 8))

        # Y gridlines at a round dB step.
        step = 60.0
        for cand in (10.0, 20.0, 40.0, 60.0):
            if (y_hi - y_lo) / cand <= 8:
                step = cand
                break
        first = int(np.ceil(y_lo / step))
        last = int(np.floor(y_hi / step))
        for k in range(first, last + 1):
            d = k * step
            y = y_of(d)
            c.create_line(ml, y, ml + plot_w, y, fill=GRID)
            ty = min(max(y, mt + 6), mt + plot_h - 4)
            c.create_text(ml - 6, ty, text=f"{d:g}", fill=AXIS, anchor="e",
                          font=(MONO, 8))
        c.create_text(ml - 4, mt - 4, text="dB/mV", fill=AXIS, anchor="se",
                      font=(MONO, 8))

        pts = []
        for f, amp in zip(freqs, amps_safe):
            pts.append(x_of(f))
            pts.append(y_of(20.0 * np.log10(amp)))
        if len(pts) >= 4:
            c.create_line(*pts, fill=TRACE, width=1)

        searchable = (freqs >= peak_min_freq) if peak_min_freq else np.ones_like(freqs, dtype=bool)
        if not searchable.any():
            searchable = np.ones_like(freqs, dtype=bool)
        peak_i = int(np.argmax(np.where(searchable, amps_mv, -np.inf)))
        peak_f, peak_amp = float(freqs[peak_i]), float(amps_mv[peak_i])
        x = x_of(peak_f)
        c.create_line(x, mt, x, mt + plot_h, fill=PEAK, dash=(2, 3))
        anchor = "nw" if x < ml + plot_w * 0.7 else "ne"
        c.create_text(x, mt + 2, text=self._fmt_hz(peak_f), fill=PEAK,
                      anchor=anchor, font=(MONO, 8))

        return peak_f, peak_amp

    def _draw_spectrum_time(self, px, c):
        """Power spectrum of one pixel's raw value across the accumulated
        frame history: one sample per frame.

        Distinct from _draw_spectrum_space, which operates within one
        frame across pixels at the 50 kHz pixel readout rate. This
        operates across frames, at the frame rate, so it can only
        resolve periodicities up to half the frame rate, a few Hz to a
        few tens of Hz on this instrument: vibration, thermal cycling, a
        flickering source, or a beat against the frame rate itself. It
        cannot show the 17.68 kHz interferer directly; that frequency is
        already above this axis's Nyquist limit before any data is
        collected, which is exactly why it took a spatial rather than a
        temporal analysis to find (see ISSUES.md entry 31).
        """
        wl = self.cal.axis(self.pixels)
        name = f"pixel {px}"
        if wl is not None:
            name += f"  ({wl[px]:.2f} nm)"

        if not self.pixel_history:
            self.hist_label.config(text=f"{name}   collecting...")
            return

        raw = np.array([frame[px] for frame in self.pixel_history],
                       dtype=np.float64)
        n = raw.size
        finite = np.isfinite(raw)
        n_finite = int(finite.sum())
        # An FFT needs a fair number of points to resolve anything; below
        # this it would show mostly window shape rather than signal.
        if n_finite < 16:
            self.hist_label.config(
                text=f"{name}   n={n_finite} sample(s), collecting"
                     f" ({self.pixel_history.maxlen} max)...")
            return

        # Captured once and held fixed rather than re-read every redraw.
        # self.fps is a live measurement that drifts by a few percent
        # frame to frame; recomputing the axis from it on every redraw
        # made the whole plot visibly rescale continuously. Reset to
        # None (recaptured here) whenever the history is cleared, which
        # already happens on reconnect and on any exposure change, so it
        # cannot go stale silently.
        if self.hist_fs is None:
            self.hist_fs = self._sample_rate_hz()
        fs = self.hist_fs
        if not fs or fs <= 0:
            self.hist_label.config(
                text=f"{name}   waiting for a stable frame rate...")
            return

        # Missing-pixel frames are interpolated across rather than
        # dropped, the same approach as notch_pattern(): dropping samples
        # would collapse the time axis and invalidate the frequency
        # scale, since an FFT assumes uniform spacing.
        idx = np.arange(n)
        work = raw.copy()
        if not finite.all():
            work[~finite] = np.interp(idx[~finite], idx[finite], raw[finite])
        mv = work * 1000.0
        mv = mv - mv.mean()

        # Hann window to control spectral leakage. Divided back out by
        # its own sum so bin amplitude reads as true mV, comparable to
        # the temporal standard deviation shown on the histogram view,
        # rather than an arbitrary window-dependent unit.
        window = np.hanning(n)
        wsum = window.sum()
        spectrum = np.abs(np.fft.rfft(mv * window)) / max(wsum, 1e-12)
        spectrum[1:] *= 2.0  # single-sided: fold the negative half back in
        freqs = np.fft.rfftfreq(n, d=1.0 / fs)

        # DC (the mean, already subtracted) is excluded: log(0) is
        # undefined and a residual there is not a frequency component.
        result = self._draw_log_spectrum(c, freqs[1:], spectrum[1:])

        df = fs / n
        text = (f"{name}   n={n_finite}/{n}   fs={fs:.2f} Hz   "
               f"df={df:.3f} Hz   Nyquist={fs / 2:.2f} Hz")
        if result is not None:
            pf, pa = result
            text += f"   peak {pf:.3f} Hz / {pa:.4f} mV"
        self.hist_label.config(text=text)

    def _draw_spectrum_space(self, c):
        """Power spectrum of the video signal WITHIN a frame, across the
        whole array, averaged over recent frames.

        This is the axis that reaches into the kHz range: pixels are
        read out at the 50 kHz pixel clock, not the frame rate, so this
        resolves frequencies up to that rate's 25 kHz Nyquist limit. It
        is the same technique that found the 17.68 kHz interferer in
        ISSUES.md entry 31, applied live to whatever is currently on the
        array rather than to a captured .npy file.

        Uses the whole valid array (masked ends excluded) rather than a
        window around a clicked pixel, and needs no pixel selected at
        all: entry 31 established the interferer is uniform across the
        array, so there is no location for a click to narrow down, and
        the full array gives about 4x the frequency resolution a 256
        pixel window would (roughly 49 Hz bins instead of 195 Hz). An
        earlier version centred a small window on whatever pixel was
        clicked; that was leftover from a "maybe it's localised"
        assumption this project had already ruled out by the time this
        view was built.

        Sourced from self.pixel_history_raw, not self.pixel_history.
        processing.notch_pattern() removes exactly the frequency band
        this view exists to show, and it is on by default; confirmed
        live that with the corrected buffer, this view could not see the
        interferer at all, and its "peak" was the array's own
        low-frequency illumination shape instead. Correction toggles
        (dark, flat, notch, mask) describe what should happen to the
        displayed and exported spectrum, not a claim that the underlying
        signal has no other structure worth being able to see.

        Averaged over SPACE_SPEC_FRAMES periodograms rather than shown
        for a single frame, because the interferer's phase is random
        frame to frame (that randomness is what made it arcsine
        distributed in the histogram view, when read from the corrected
        buffer where it is not removed); averaging magnitude spectra
        across frames is standard practice for exactly this situation
        (Welch's method, here with 100 percent overlap since each frame
        is an independent full window) and turns a noisy single-frame
        estimate into a stable one.
        """
        if not self.pixel_history_raw:
            self.hist_label.config(text="collecting...")
            return

        n_px = self.pixels
        # Pixels 0 and 1023 are non-physical readout artifacts regardless
        # of the Mask end pixels toggle (see processing.mask_end_pixels);
        # excluded here unconditionally since this view bypasses that
        # toggle entirely by design.
        lo = processing.MASK_HEAD
        hi = n_px - processing.MASK_TAIL  # exclusive
        win = hi - lo
        if win < 32:
            self.hist_label.config(
                text="array too narrow for a spatial spectrum")
            return

        frames = list(self.pixel_history_raw)[-SPACE_SPEC_FRAMES:]
        idx = np.arange(win)
        window = np.hanning(win)
        wsum = window.sum()
        acc = None
        used = 0
        for frame in frames:
            seg = frame[lo:hi]
            finite = np.isfinite(seg)
            # A frame missing half or more of the array would need more
            # invention than interpolation can honestly do; skip it
            # rather than let it distort the average.
            if finite.sum() < win * 0.5:
                continue
            work = seg.copy()
            if not finite.all():
                work[~finite] = np.interp(idx[~finite], idx[finite],
                                          seg[finite])
            mv = work * 1000.0
            # Subtract a baseline anchored to the array's own two ends,
            # not just the mean, for the same reason as
            # notch_pattern()'s baseline (ISSUES.md entries 31, 32, 33):
            # the illumination profile is real, large, and slopes across
            # the array, and a slope left in is a step at the FFT's wrap
            # point that lands as a huge, spurious low-frequency
            # component swamping everything real above it.
            edge = max(1, win // 16)
            lo_v = float(np.median(mv[:edge]))
            hi_v = float(np.median(mv[-edge:]))
            base = lo_v + (hi_v - lo_v) * idx / (win - 1)
            mv = mv - base
            mag = np.abs(np.fft.rfft(mv * window)) / max(wsum, 1e-12)
            mag[1:] *= 2.0
            acc = mag if acc is None else acc + mag
            used += 1

        if used == 0:
            self.hist_label.config(text="no usable frames in this history")
            return
        spectrum = acc / used

        pixel_period_us = getattr(self.device.info, "pixel_period_us", 0) or 20.0
        fs = 1e6 / pixel_period_us
        freqs = np.fft.rfftfreq(win, d=pixel_period_us * 1e-6)

        result = self._draw_log_spectrum(c, freqs[1:], spectrum[1:],
                                        peak_min_freq=SPACE_SPEC_PEAK_MIN_HZ)

        df = fs / win
        text = (f"whole array   [raw, uncorrected]   px{lo}-{hi - 1} "
               f"({win}px)   frames={used}/{len(frames)}   "
               f"fs={fs / 1000:.2f} kHz   df={df:.0f} Hz   "
               f"Nyquist={fs / 2000:.2f} kHz")
        if result is not None:
            pf, pa = result
            text += f"   peak {pf / 1000:.3f} kHz / {pa:.4f} mV"
        self.hist_label.config(text=text)

    # -- box zoom --
    #
    # Press records the drag origin and opens a rubber-band rectangle.
    # Motion resizes it. Release converts the rectangle to data
    # coordinates using the view that was active when the drag STARTED
    # (self.view_x / self.view_y are not touched until release, so the
    # mapping used throughout the drag stays consistent with what is
    # on screen) and installs it as the new view.
    #
    # A drag shorter than _drag_threshold in both directions is treated as
    # a plain click instead, so the pre-existing single-click behaviour
    # (setting the calibration pixel field) still works without requiring
    # pixel-perfect stillness.

    def _draw_drag_rect(self):
        """(Re)draw the selection box from self._drag_last.

        Called from _on_press/_on_drag as the mouse moves, and again at
        the end of _draw_plot. The plot redraws on its own every time a
        new frame arrives (POLL_MS, independent of the mouse), and that
        redraw starts with a full canvas clear, which would otherwise
        erase the box within milliseconds of it being drawn while the
        device is streaming. Recreating it after every redraw is what
        keeps the selection visible for the whole drag instead of just
        flickering into view.
        """
        if self._drag_rect is not None:
            self.plot.delete(self._drag_rect)
            self._drag_rect = None
        if self._drag_last is None:
            return
        self._drag_rect = self.plot.create_rectangle(
            *self._drag_last, outline=TRACE_REF, width=2,
            dash=(4, 2), fill=TRACE_REF, stipple="gray25")

    def _on_press(self, event):
        if self._current_data() is None:
            return
        x0, y0, x1, y1 = self._plot_rect()
        cx = min(max(event.x, x0), x1)
        cy = min(max(event.y, y0), y1)
        self._drag_start = (cx, cy)
        self._drag_last = (cx, cy, cx, cy)
        self._draw_drag_rect()

    def _on_drag(self, event):
        if self._drag_start is None:
            return
        x0, y0, x1, y1 = self._plot_rect()
        cx = min(max(event.x, x0), x1)
        cy = min(max(event.y, y0), y1)
        sx, sy = self._drag_start
        self._drag_last = (sx, sy, cx, cy)
        self._draw_drag_rect()

    def _on_release(self, event):
        if self._drag_start is None:
            return
        sx, sy = self._drag_start
        x0, y0, x1, y1 = self._plot_rect()
        cx = min(max(event.x, x0), x1)
        cy = min(max(event.y, y0), y1)

        self._drag_last = None
        self._draw_drag_rect()
        self._drag_start = None

        if (abs(cx - sx) < self._drag_threshold
                and abs(cy - sy) < self._drag_threshold):
            self._on_click(event)
            return

        data = self._current_data()
        if data is None:
            return

        x_lo, x_hi, y_lo, y_hi = self._view(data)
        h = y1 - y0

        def px_of(canvas_x):
            return x_lo + (canvas_x - x0) / (x1 - x0) * (x_hi - x_lo)

        def v_of(canvas_y):
            return y_lo + (1.0 - (canvas_y - y0) / h) * (y_hi - y_lo)

        px_a, px_b = sorted((px_of(sx), px_of(cx)))
        v_a, v_b = sorted((v_of(sy), v_of(cy)))

        px_a = max(0.0, px_a)
        px_b = min(float(len(data)), px_b)
        if px_b - px_a < 1.0:
            return  # narrower than one pixel: not a meaningful selection

        self.view_x = (px_a, px_b)
        self.view_y = (v_a, v_b)
        self._redraw()

    def _reset_zoom(self):
        if self.view_x is None and self.view_y is None:
            return
        self.view_x = None
        self.view_y = None
        self._redraw()

    def _update_zoom_label(self, volts):
        if self.view_x is None and self.view_y is None:
            self.zoom_label.config(text="")
            self.home_btn.config(state="disabled")
            return
        x_lo, x_hi, y_lo, y_hi = self._view(volts)
        self.zoom_label.config(
            text=(f"zoom: px {x_lo:.0f}-{x_hi:.0f}  "
                  f"{y_lo * 1000:.0f}-{y_hi * 1000:.0f} mV"))
        self.home_btn.config(state="normal")

    # -------------------------------------------------------- calibration

    def _add_cal_point(self):
        try:
            px = float(self.cal_px.get())
            wl = float(self.cal_wl.get())
        except ValueError:
            messagebox.showwarning(APP_NAME,
                                   "Enter a pixel and a wavelength in nm.")
            return
        if wl <= 0:
            return
        self.cal.add_point(px, wl)
        self.cal_wl.set("")
        self._persist_cal()
        self._refresh_cal()
        self._redraw()

    def _remove_cal_point(self):
        try:
            px = float(self.cal_px.get())
        except ValueError:
            return
        if self.cal.remove_nearest(px):
            self._persist_cal()
            self._refresh_cal()
            self._redraw()

    def _clear_cal(self):
        self.cal.clear()
        self._persist_cal()
        self._refresh_cal()
        self._redraw()

    def _persist_cal(self):
        self.settings.set("calibration", self.cal.to_dict())
        self.settings.save()

    def _refresh_cal_device_label(self):
        """Say whether the instrument agrees with what is on screen.

        The point of the store is that the calibration follows the
        instrument. A window that has edited its points and not sent them is
        the one state where that is not true, so it is the state worth
        naming.
        """
        if not hasattr(self, "cal_device_label"):
            return
        info = self.device.info
        if not self.device.connected:
            text, colour = "device not connected", FG_DIM
        elif not info.supports_calibration:
            text, colour = "firmware has no calibration store", FG_DIM
        elif not info.cal_store:
            text, colour = "device store unavailable", MISSING
        elif info.cal_n < 2:
            text, colour = "no calibration found", MISSING
        elif self.cal.matches_device_text(self.device.cal_last.points):
            text, colour = f"device holds these {info.cal_n} points", OK
        else:
            text, colour = (f"device holds {info.cal_n} different points",
                            PEAK)
        self.cal_device_label.config(text=text, foreground=colour)

    def _refresh_cal(self):
        self._refresh_cal_device_label()
        self.cal_text.delete("1.0", "end")
        self.cal_text.insert("end", self.cal.describe() + "\n\n")
        if not self.cal.points:
            return
        self.cal_text.insert("end",
                             "   pixel        nm       fit-nm    residual\n")
        for px, wl, res in (self.cal.residuals() or
                            [(p, w, float('nan')) for p, w in self.cal.points]):
            fit = wl + res if np.isfinite(res) else float("nan")
            self.cal_text.insert(
                "end", f"{px:8.2f} {wl:10.3f} {fit:10.3f} {res:10.3f}\n")

    # --------------------------------------------------------------- open

    def _open_spectrum_file(self):
        """Open a spectrum from this computer rather than from the device.

        Two forms are read. A .STK file is the record the firmware writes,
        so it is decoded through spectrometer/stkfile.py, the same code the
        standalone reader uses, and it arrives complete with its capture
        counters. A .txt file is one this program wrote, and carries only
        the columns.

        Neither needs a device. A card read in a card reader, or a file sent
        by someone else, opens exactly like a stored spectrum does.
        """
        path = filedialog.askopenfilename(
            title="Open spectrum",
            filetypes=[("Spectrum files", "*.stk *.txt"),
                       ("Device files", "*.stk"),
                       ("Text spectra", "*.txt"),
                       ("All files", "*.*")])
        if not path:
            return
        name = Path(path).name
        try:
            if path.lower().endswith(".stk"):
                result, codec, stored = stkfile.load(path)
                volts = self._stack_to_volts(result)
                self.frozen = volts
                self.stack_display = result
                self._log(f"{name}: " + result.summary())
                self._log(f"      stored {stored} bytes, codec {codec}")
            else:
                volts = self._read_text_spectrum(path)
                self.frozen = volts
                self.stack_display = None
                finite = int(np.count_nonzero(np.isfinite(volts)))
                self._log(f"{name}: {finite} of {len(volts)} pixels")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not open {name}: {exc}")
            return
        self._update_ref_label()
        self._redraw()

    def _read_text_spectrum(self, path):
        """The volts column of a file this program wrote.

        Header lines start with "#" and are skipped. The volts column is the
        second field when there is no wavelength column and the third when
        there is, which the "# columns:" line states; the field count is
        used rather than that line so a file edited by hand still reads.
        """
        volts = []
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                fields = line.split()
                if len(fields) < 2:
                    continue
                try:
                    volts.append(float(fields[2] if len(fields) >= 4
                                       else fields[1]))
                except ValueError:
                    continue
        if not volts:
            raise ValueError("no data rows")
        out = np.array(volts, dtype=np.float64)
        if out.size != self.pixels:
            # Not an error. A file from a different detector still plots;
            # what breaks is comparing it against a reference taken here, so
            # the mismatch is said out loud rather than silently padded.
            self._log(f"[!] the file holds {out.size} pixels, this device "
                      f"has {self.pixels}")
        return out

    # -------------------------------------------------- calibration store

    def _cal_device_ready(self):
        if not self.device.connected:
            messagebox.showwarning(APP_NAME, "Connect to a device first.")
            return False
        if not self.device.info.supports_calibration:
            messagebox.showwarning(
                APP_NAME,
                "This firmware cannot store a calibration.\n\n"
                "Update it. The calibration still works here; it just does "
                "not follow the instrument.")
            return False
        return True

    def _cal_push_to_device(self):
        """Write the current points into the instrument's own flash."""
        if not self._cal_device_ready():
            return
        if len(self.cal.points) < 2:
            messagebox.showwarning(
                APP_NAME, "Add at least two calibration points first.")
            return
        try:
            text = self.cal.to_device_text()
        except ValueError as exc:
            messagebox.showwarning(APP_NAME, f"Cannot send: {exc}.")
            return
        try:
            self.device.cal_set(text)
        except DeviceError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        self._log(f"sent {len(self.cal.points)} calibration points to the "
                  f"device")

    def _cal_pull_from_device(self):
        """Ask the device for its calibration. The reply arrives in
        _on_cal_event."""
        if not self._cal_device_ready():
            return
        self.cal_pull_pending = "menu"
        try:
            self.device.cal_get()
        except DeviceError as exc:
            self.cal_pull_pending = None
            messagebox.showerror(APP_NAME, str(exc))

    def _cal_clear_on_device(self):
        if not self._cal_device_ready():
            return
        if not messagebox.askyesno(
                APP_NAME,
                "Erase the calibration stored on the device?\n\n"
                "The points in this window are not affected."):
            return
        try:
            self.device.cal_clear()
        except DeviceError as exc:
            messagebox.showerror(APP_NAME, str(exc))

    def _cal_export_file(self):
        path = filedialog.asksaveasfilename(
            title="Export calibration", defaultextension=".json",
            filetypes=[("Calibration", "*.json")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(self.cal.to_dict(), fh, indent=2)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Export failed: {exc}")
            return
        self._log(f"calibration written to {Path(path).name}")

    def _cal_import_file(self):
        path = filedialog.askopenfilename(
            title="Import calibration", filetypes=[("Calibration", "*.json"),
                                                   ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self.cal.from_dict(data)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Import failed: {exc}")
            return
        self._persist_cal()
        self._refresh_cal()
        self._redraw()
        self._log(f"calibration read from {Path(path).name}: "
                  f"{self.cal.describe()}")

    def _on_cal_event(self, info):
        """One "K cal" reply from the device.

        A reply is produced by every calibration command, not only by a
        read, so this has to tell an answer to "load from the device" apart
        from the confirmation of a write. cal_pull_pending says which, and
        distinguishes the read the Calibration menu asked for ("menu") from
        the one every connect makes ("auto"). Only the first reports an
        empty device in a dialog: an instrument that has never been
        calibrated would otherwise interrupt every connect to say so.
        """
        self.device.info.cal_n = info.count
        self.device.info.cal_store = int(info.store)
        pulling = getattr(self, "cal_pull_pending", None)
        self.cal_pull_pending = None
        self._log("device calibration: " + info.summary())
        if not info.store:
            self._log("[!] the device has no calibration store; a "
                      "calibration sent to it will not survive a power cycle")
        if not pulling:
            self._refresh_cal()
            return
        if not info.present:
            if pulling == "menu":
                messagebox.showinfo(APP_NAME,
                                    "The device holds no calibration.")
            self._refresh_cal()
            return
        n = self.cal.from_device_text(info.points)
        self._persist_cal()
        self._refresh_cal()
        self._redraw()
        self._log(f"loaded {n} calibration points from the device: "
                  f"{self.cal.describe()}")

    # ------------------------------------------------------------- export

    def _save_spectrum(self):
        data = self.frozen if self.frozen is not None else self.display
        if data is None:
            messagebox.showwarning(APP_NAME, "Nothing to save.")
            return
        path = filedialog.asksaveasfilename(
            title="Save spectrum", defaultextension=".txt",
            filetypes=[("Text", "*.txt"), ("All files", "*.*")],
            initialfile=time.strftime("spectrum_%Y%m%d_%H%M%S.txt"))
        if not path:
            return
        wl = self.cal.axis(len(data))
        res = self.stack_display
        if res is not None:
            frames = res.frames_stacked
            elapsed = res.duration_ms / 1000.0
        else:
            frames = max(1, self.accum.depth)
            elapsed = self.accum.depth / self.fps if self.fps else 0.0
        try:
            export.write_spectrum(
                path, np.arange(len(data)), data, wavelengths=wl,
                info=self.device.info, frames=frames, elapsed_s=elapsed,
                dark_applied=self.use_dark.get() and self.dark is not None,
                flat_applied=self.use_flat.get() and self.flat is not None,
                notch_applied=True,
                calibration=self.cal, peaks=self.peaks, stack=res)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Save failed: {exc}")
            return
        self._log(f"saved {Path(path).name}")

    # ------------------------------------------------------------- console

    def _log(self, text, tag=None):
        if not hasattr(self, "diag_text"):
            return
        self.console_records.append((text, tag))
        if not self._console_filter_enabled(tag):
            return
        self._insert_console_line(text, tag)

    @staticmethod
    def _log_tag_for(kind, text):
        """The console style for one device line.

        Only the hardware tests need the text as well as the prefix. Every
        other line's meaning is fixed by its prefix, but an "H " line
        reports a verdict, and a verdict has to be coloured by what it says
        rather than by what kind of line it is.
        """
        if kind == "test" and "FAIL" in text:
            return "testfail"
        return kind

    def _console_filter_enabled(self, tag):
        filters = {
            "stat": getattr(self, "show_stats", None),
            # With the frame statistics, because it is also one line per
            # frame. That checkbox is off by default, which is why these no
            # longer flood the console.
            "trig": getattr(self, "show_stats", None),
            "error": getattr(self, "show_errors", None),
            "info": getattr(self, "show_status", None),
            "cap": getattr(self, "show_status", None),
            "sent": getattr(self, "show_commands", None),
            "local": getattr(self, "show_commands", None),
            "ack": getattr(self, "show_replies", None),
            "test": getattr(self, "show_replies", None),
            "testfail": getattr(self, "show_replies", None),
            "text": getattr(self, "show_replies", None),
            None: getattr(self, "show_replies", None),
        }
        enabled = filters.get(tag, getattr(self, "show_replies", None))
        return enabled is None or enabled.get()

    def _insert_console_line(self, text, tag):
        # Only follow the tail when the view is already at the tail.
        # Scrolling back to read something must not be undone by the next
        # frame's stats arriving.
        at_end = self.diag_text.yview()[1] > 0.995
        tags = (tag,) if tag in LOG_STYLES else ()
        self.diag_text.insert("end", text + "\n", tags)
        if int(self.diag_text.index("end-1c").split(".")[0]) > 2000:
            self.diag_text.delete("1.0", "500.0")
        if at_end:
            self.diag_text.see("end")

    def _render_console(self):
        self.diag_text.delete("1.0", "end")
        for text, tag in self.console_records:
            if self._console_filter_enabled(tag):
                tags = (tag,) if tag in LOG_STYLES else ()
                self.diag_text.insert("end", text + "\n", tags)
        self.diag_text.see("end")

    def _console_text_key(self, event):
        """Keep the log read only without making it dead.

        Copy, select all and the navigation keys still work. Anything
        that would edit the buffer moves to the entry instead, so typing
        while the log holds focus lands where the user meant it to.
        """
        if event.state & 0x4:                      # Control held: copy etc.
            return None
        if event.keysym in ("Up", "Down", "Left", "Right", "Prior", "Next",
                            "Home", "End", "Shift_L", "Shift_R"):
            return None
        self.cmd_entry.focus_set()
        if event.char and event.char.isprintable():
            self.cmd_entry.insert("insert", event.char)
        return "break"

    def _console_submit(self, event=None):
        text = self.cmd_var.get().strip()
        self.cmd_var.set("")
        if not text:
            return "break"
        # Record before the command runs, so one that fails is still
        # recallable with Up and can be corrected rather than retyped.
        if not self.cmd_history or self.cmd_history[-1] != text:
            self.cmd_history.append(text)
            del self.cmd_history[:-CONSOLE_HISTORY_LEN]
        self.cmd_index = len(self.cmd_history)
        self.cmd_draft = ""
        self._log("> " + text, tag="sent")
        if not self._console_local(text):
            self._console_send(text)
        return "break"

    def _console_local(self, text):
        """Run the commands this application answers itself.

        Returns True if the command was handled here and must not go to
        the device.
        """
        word = text.split()[0].lower()
        if word in ("help", "?"):
            self._console_help()
            return True
        if word in ("clear", "cls"):
            self._console_clear()
            return True
        if word == "history":
            if not self.cmd_history:
                self._log("  no history", tag="local")
            for n, item in enumerate(self.cmd_history, 1):
                self._log(f"  {n:3d}  {item}", tag="local")
            return True
        return False

    def _console_send(self, text):
        try:
            self.device.send(text)
        except DeviceError as exc:
            self._log(f"  not sent: {exc}", tag="error")
            return
        # A command that changes frame timing also changes what the
        # Acquisition panel reports. Ask the device to identify itself
        # again so the panel follows, instead of showing a rate that is
        # no longer set.
        if text.split()[0].lower() in RESYNC_COMMANDS:
            self.root.after(200, self.device.request_info)

    def _console_history_back(self, event=None):
        if not self.cmd_history:
            return "break"
        if self.cmd_index == len(self.cmd_history):
            self.cmd_draft = self.cmd_var.get()
        self.cmd_index = max(0, self.cmd_index - 1)
        self.cmd_var.set(self.cmd_history[self.cmd_index])
        self.cmd_entry.icursor("end")
        return "break"

    def _console_history_forward(self, event=None):
        if not self.cmd_history:
            return "break"
        self.cmd_index = min(len(self.cmd_history), self.cmd_index + 1)
        if self.cmd_index == len(self.cmd_history):
            # Past the newest entry is whatever was half typed before the
            # first Up, not an empty line.
            self.cmd_var.set(self.cmd_draft)
        else:
            self.cmd_var.set(self.cmd_history[self.cmd_index])
        self.cmd_entry.icursor("end")
        return "break"

    def _console_complete(self, event=None):
        """Complete the command word when Tab is pressed.

        Only the first word completes. Arguments are values, and a guess
        at a value would be worse than no help at all.
        """
        text = self.cmd_var.get()
        if " " in text.strip():
            return "break"
        prefix = text.strip().lower()
        names = ([n for n, _, _ in DEVICE_COMMANDS]
                 + ["help", "clear", "history"])
        hits = [n for n in names if n.startswith(prefix)]
        if not hits:
            return "break"
        if len(hits) == 1:
            # A trailing space only where an argument follows, so a bare
            # command is ready for Enter.
            takes_arg = any(n == hits[0] and arg
                            for n, arg, _ in DEVICE_COMMANDS)
            self.cmd_var.set(hits[0] + (" " if takes_arg else ""))
        else:
            # Fill in as far as the candidates agree, then list them.
            common = hits[0]
            for h in hits[1:]:
                while not h.startswith(common):
                    common = common[:-1]
            self.cmd_var.set(common)
            self._log("  " + "  ".join(hits), tag="local")
        self.cmd_entry.icursor("end")
        return "break"

    def _console_help(self):
        self._log("firmware commands", tag="local")
        for name, arg, meaning in DEVICE_COMMANDS:
            label = (name + " " + arg).strip()
            self._log(f"  {label:<16} {meaning}", tag="local")
        self._log("console commands", tag="local")
        for label, meaning in (
                ("help", "This list."),
                ("clear", "Empty the log. Ctrl+L does the same."),
                ("history", "Show the commands sent this session.")):
            self._log(f"  {label:<16} {meaning}", tag="local")
        self._log("  Up and Down recall history. Tab completes a command.",
                  tag="local")

    def _console_clear(self):
        self.console_records.clear()
        self.diag_text.delete("1.0", "end")

    def _console_clear_event(self, event=None):
        self._console_clear()
        return "break"

    def _show_legend(self):
        win = tk.Toplevel(self.root)
        win.title("Diagnostic fields")
        win.configure(bg=BG)
        win.geometry("760x560")
        # Prose, so the interface face. The field names inside it keep the
        # monospace face, because they are the identifiers the console
        # prints and they have to be recognisable as the same thing.
        txt = tk.Text(win, font=(UI, 10), bg=PANEL, fg=FG,
                      wrap="word", padx=10, pady=10, spacing1=1)
        scroll = ttk.Scrollbar(win, command=txt.yview)
        txt.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        txt.pack(fill="both", expand=True)

        txt.insert("end", "Per-frame diagnostics\n\n", "title")
        txt.insert("end",
                   "One line per frame. The healthy value is given for each "
                   "field.\n\n")
        for name, healthy, meaning in DIAGNOSTIC_LEGEND:
            txt.insert("end", f"{name}", "field")
            txt.insert("end", f"   healthy: {healthy}\n", "healthy")
            txt.insert("end", f"    {meaning}\n\n")
        txt.tag_config("field", font=(MONO, 10, "bold"))
        txt.tag_config("title", font=(UI_SEMI, 13))
        txt.tag_config("healthy", foreground=FG_DIM)
        txt.configure(state="disabled")

    def _draw_logo(self, parent, size):
        """The mark and the wordmark, on one canvas.

        A square of five stripes, then the name. The square is exactly as
        tall as a capital letter and sits on the same baseline, which is
        what makes the pair read as one object rather than as a picture
        placed next to some text.

        Tk can report a font's ascent but cannot measure a glyph, so the
        cap height comes from the ratio in typography.py rather than from
        anything measurable here.
        """
        word = tkfont.Font(family=UI, size=size, weight="bold")
        ascent = word.metrics("ascent")
        descent = word.metrics("descent")
        cap = max(8, round(ascent * typography.CAP_OVER_ASCENT))
        gap = max(3, round(cap * 0.34))
        text_w = word.measure(APP_NAME)

        width = cap + gap + text_w
        height = ascent + descent
        canvas = tk.Canvas(parent, width=width, height=height, bg=BG,
                           highlightthickness=0, takefocus=0)

        # Stripe edges are placed from the exact fraction rather than by
        # adding a rounded height five times, so no seam or overlap can
        # accumulate down the square.
        top = ascent - cap
        for i, colour in enumerate(LOGO_STRIPES):
            y0 = top + round(cap * i / len(LOGO_STRIPES))
            y1 = top + round(cap * (i + 1) / len(LOGO_STRIPES))
            canvas.create_rectangle(0, y0, cap, y1, fill=colour, width=0)

        canvas.create_text(cap + gap, 0, text=APP_NAME, anchor="nw",
                           font=word, fill=FG)
        return canvas

    def _show_about(self):
        """Who made this, and what it is called."""
        existing = getattr(self, "_about_win", None)
        if existing is not None and existing.winfo_exists():
            existing.deiconify()
            existing.lift()
            existing.focus_set()
            return

        win = tk.Toplevel(self.root)
        self._about_win = win
        win.title(f"About")
        win.configure(bg=BG)
        win.resizable(False, False)
        win.transient(self.root)

        body = ttk.Frame(win, padding=(30, 26, 30, 20))
        body.pack(fill="both", expand=True)

        self._draw_logo(body, 30).pack(anchor="w")
        ttk.Label(body, text=AUTHOR, style="Dim.TLabel",
                  font=(UI, 11)).pack(anchor="w", pady=(16, 0))
        ttk.Button(body, text="Close", width=10,
                   command=win.destroy).pack(anchor="e", pady=(24, 0))

        win.bind("<Escape>", lambda e: win.destroy())

        # Centred on the window it came from, not on the screen. A dialog
        # that opens over its parent is a dialog you do not have to look
        # for on a second monitor.
        win.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width()
                                       - win.winfo_width()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height()
                                       - win.winfo_height()) // 3
        win.geometry(f"+{max(0, x)}+{max(0, y)}")
        win.focus_set()

    def _copy_diag(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.diag_text.get("1.0", "end"))
        self.root.update()

    def _update_diag_summary(self):
        s = self.last_stats
        if s is None:
            return
        self.diag_summary.config(
            text=(f"seq {s.seq}   pulses {s.pulses}   miss {s.miss}   "
                  f"period {s.period_us}us   fifo {s.fifo}/8\n"
                  f"gap {s.gap_min}-{s.gap_max}us   late {s.late_us}us   "
                  f"dur {s.dur_us}us   maxread {s.maxread_us}us\n"
                  f"wdog {s.wdog}   nostart {s.nostart}   retry {s.retry}   "
                  f"stale {s.stale}   txdrop {s.txdrop}"))

    def on_close(self):
        self._persist_cal()
        self.device.disconnect()
        self.root.destroy()


def main():
    # Must happen before the window exists. Windows reads it when the taskbar
    # button is created; setting it afterwards has no effect.
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "s3903.helios")
        except Exception:
            pass

    root = tk.Tk()
    app = SpectrometerApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
