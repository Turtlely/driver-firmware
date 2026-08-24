"""Spectrometer application for the S3903-1024Q detector.

Layout is fixed and functional: connection and acquisition controls on the
left, plot and readouts on the right, tabs for the secondary functions.
Everything needed during a measurement is visible without navigating, and
the tabs hold work done once per session rather than per measurement.

The user interface never touches the serial port. Device owns the port on
its own thread and the interface polls queues on a timer, so a slow redraw
cannot stall acquisition and a disconnect cannot deadlock the window.
"""

import time
import tkinter as tk
from collections import deque
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import numpy as np

from . import export, processing
from .calibration import Calibration, Settings
from .device import (MAX_CODE, Device, DeviceError, find_devices)

APP_NAME = "spectrometer"
POLL_MS = 40

PLOT_W = 1024
PLOT_H = 300
STRIP_H = 44
MARGIN_L = 62
MARGIN_R = 12
MARGIN_T = 10
MARGIN_B = 26

# Depth of the per-pixel value history kept for the distribution tab, in
# frames. Independent of the display averaging depth (Average control),
# which is a running mean and would smooth out the very noise this history
# exists to show. At a typical 20 to 40 Hz frame rate this is 12 to 25
# seconds of samples, enough for a stable histogram without keeping every
# frame since connection.
PIXEL_HISTORY_LEN = 500
HIST_W = 380
HIST_H = 190
HIST_MARGIN_L = 50
HIST_MARGIN_R = 26
HIST_MARGIN_T = 14
HIST_MARGIN_B = 24

# How many recent frames to average the within-frame ("per-frame")
# spectrum's power over. Averaging periodograms, not the time-domain
# signal, is standard (Welch's method with 100% overlap): the
# interferer's phase is random per frame, so the magnitude averages down
# to a stable estimate instead of cancelling.
SPACE_SPEC_FRAMES = 60
# Peak search on the per-frame spectrum starts here, not at 0 Hz.
# processing.notch_pattern() established that 99.96 percent of a real
# frame's spectral energy sits below 0.30 cycles/px (15 kHz at this
# instrument's 20 us pixel period); measured live, the array's own real,
# low-frequency illumination content routinely has more raw amplitude
# than the 17.68 kHz interferer has anywhere. The full curve is still
# drawn from its lowest bin; only the automatic peak marker is
# restricted, so it flags the interferer's band rather than the fact
# that the array is illuminated.
SPACE_SPEC_PEAK_MIN_HZ = 15000.0

# Light palette. Chosen for contrast on a plot that is read for small
# amplitude differences, not for appearance: the trace is the darkest
# element, grid lines are light enough not to compete with it, and the
# fault colour is distinguishable from the trace for the common forms of
# colour vision deficiency.
BG = "#ffffff"
PANEL = "#f2f2f4"
FG = "#1f2328"
FG_DIM = "#57606a"
GRID = "#dcdce2"
GRID_MAJOR = "#c0c0c8"
AXIS = "#57606a"
TRACE = "#0b4f9e"
TRACE_REF = "#8250df"
MISSING = "#cf222e"
PEAK = "#bc4c00"
CURSOR = "#8c959f"
STRIP_BORDER = "#c0c0c8"


# Every field the firmware reports, what it counts, and what a value other
# than the healthy one indicates. Kept next to the code that displays it so
# the two cannot drift apart.
DIAGNOSTIC_LEGEND = [
    ("pulses", "1024",
     "Pixels captured this frame. The CPLD emits exactly 1024 triggers per "
     "scan, so anything less means triggers were not seen."),
    ("raw", "= pulses",
     "Triggers counted by a second state machine that applies no debounce "
     "at all. Comparing it against pulses separates two causes: raw well "
     "above pulses means the debounce window is discarding real triggers; "
     "raw also short means they never reached the device."),
    ("miss", "0",
     "Pixel slots left empty. Those slots carry the missing-pixel sentinel "
     "rather than a value, so they are excluded from statistics instead of "
     "being averaged in as zero."),
    ("period", "20 us",
     "Measured interval between pixel triggers, taken as the median across "
     "the frame. This is the CPLD pixel clock, not the frame rate. It "
     "should be rock steady; drift means the timing reference moved."),
    ("wdog", "0",
     "The scan watchdog fired: a scan started but did not deliver all its "
     "pixels within 100 ms."),
    ("nostart", "0",
     "The CPLD ignored the START pulse and the retries were exhausted. "
     "Distinct from wdog: nothing was emitted at all."),
    ("retry", "0",
     "Extra START pulses issued because the first was ignored. Nonzero "
     "with nostart at 0 means the retry recovered the frame, which is the "
     "system working as intended."),
    ("early", "= pulses",
     "Reads where the trigger line had already released before the SPI "
     "transfer finished. Saturated on this hardware and benign: the "
     "converter holds its result until the next conversion, so the data is "
     "still valid. Useful only in reverse, if it ever drops well below the "
     "pixel count."),
    ("fifo", "1 to 2",
     "Deepest trigger backlog seen, out of 8. Approaching 8 means the "
     "processor is not keeping up with the trigger rate and is close to "
     "dropping one."),
    ("gap", "17 to 23 us",
     "Shortest and longest interval between accepted triggers. Spread "
     "comes from measuring on the processor rather than from the sensor; "
     "a wide spread is normal, a shifted centre is not."),
    ("late", "under ~1000 us",
     "How overdue the frame was against its own schedule. Large values "
     "mean something is stalling the acquisition loop."),
    ("dur", "~20500 us",
     "Wall clock time for the whole frame, dominated by the scan. Equal to "
     "the integration time only when running at the maximum rate."),
    ("maxread", "6 to 14 us",
     "Longest single ADC read this frame. Includes interrupt activity "
     "around the read, so the minimum is the true transfer time and the "
     "maximum reflects system load."),
    ("txdrop", "not rising",
     "Frames whose data was discarded because the host had not finished "
     "reading the previous one. A rising count means the link or the host "
     "cannot keep up at this frame rate."),
    ("lowbits", "all in first bucket",
     "Distribution of the two unused bits of each converter word. They are "
     "constant in healthy operation, so anything in the other buckets "
     "indicates the SPI read lost or gained a clock."),
    ("reread", "0",
     "Reads repeated because the two independent reads of the same "
     "conversion disagreed."),
    ("bad", "0",
     "Reads where three attempts all disagreed, so the value returned is a "
     "best effort rather than a confirmed measurement."),
    ("px<n>", "-",
     "The value of one nominated pixel, as a code and in millivolts. A "
     "quick sanity check that the signal is where it is expected."),
]


class SpectrometerApp:
    def __init__(self, root):
        self.root = root
        self.dev = Device()
        self.settings = Settings(
            Path.home() / ".s3903_spectrometer" / "settings.json")
        self.cal = Calibration()
        self.cal.from_dict(self.settings.get("calibration", {}))

        self.pixels = self.dev.info.pixels
        self.accum = processing.Accumulator(self.pixels, 1)
        self.dark = None
        self.flat = None
        self.live_volts = None
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
        # 17.68 kHz notch (processing.notch_pattern, on by default) would
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

        self._build()
        self._refresh_ports()
        self.root.after(POLL_MS, self._poll)

    # ---------------------------------------------------------------- UI

    def _build(self):
        self.root.title(APP_NAME)
        self.root.configure(bg=BG)
        self.root.minsize(1180, 760)

        # Window/taskbar icon. .ico, not the source .jpg: iconbitmap()
        # only accepts .ico on Windows, which is the only platform this
        # project runs on (see docs/ARCHITECTURE.md). Generated once at
        # tools/spectrometer/icon.ico from icon.jpg (Pillow, build-time
        # only) rather than loaded from the .jpg at runtime, so the
        # application keeps its numpy/pyserial-only dependency list.
        # Missing file or a platform that rejects .ico is not fatal;
        # the window just runs with Tk's default icon.
        icon_path = Path(__file__).resolve().parent / "icon.ico"
        if icon_path.exists():
            try:
                self.root.iconbitmap(default=str(icon_path))
            except tk.TclError:
                pass

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TLabelframe", background=BG, foreground=FG)
        style.configure("TLabelframe.Label", background=BG, foreground=FG_DIM)
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("TCheckbutton", background=BG, foreground=FG)
        style.configure("TNotebook", background=BG)
        style.configure("TRadiobutton", background=BG, foreground=FG)
        style.configure("TButton", foreground=FG)

        outer = ttk.Frame(self.root, padding=6)
        outer.pack(fill="both", expand=True)

        left = ttk.Frame(outer)
        left.pack(side="left", fill="y", padx=(0, 6))
        right = ttk.Frame(outer)
        right.pack(side="left", fill="both", expand=True)

        self._build_connection(left)
        self._build_acquisition(left)
        self._build_correction(left)
        self._build_readout(left)
        self._build_plot(right)
        self._build_tabs(right)

        self.status = ttk.Label(self.root, text="Disconnected", anchor="w",
                                font=("Consolas", 9))
        self.status.pack(fill="x", side="bottom")

    def _build_connection(self, parent):
        box = ttk.Labelframe(parent, text="Device", padding=6)
        box.pack(fill="x", pady=(0, 6))

        row = ttk.Frame(box)
        row.pack(fill="x")
        self.port_var = tk.StringVar()
        self.port_cb = ttk.Combobox(row, textvariable=self.port_var,
                                    width=22, state="readonly")
        self.port_cb.pack(side="left")
        ttk.Button(row, text="Rescan", width=8,
                   command=self._refresh_ports).pack(side="left", padx=3)

        row2 = ttk.Frame(box)
        row2.pack(fill="x", pady=(4, 0))
        self.connect_btn = ttk.Button(row2, text="Connect", width=12,
                                      command=self._toggle_connection)
        self.connect_btn.pack(side="left")
        ttk.Button(row2, text="Identify", width=10,
                   command=lambda: self.dev.request_info()).pack(side="left",
                                                                 padx=3)

        self.dev_label = ttk.Label(box, text="no device", font=("Consolas", 8),
                                   justify="left")
        self.dev_label.pack(fill="x", pady=(5, 0))

    def _build_acquisition(self, parent):
        box = ttk.Labelframe(parent, text="Acquisition", padding=6)
        box.pack(fill="x", pady=(0, 6))

        # Rate and integration time are the same setting in different
        # units, since exposure is the START-to-START interval. Both are
        # offered because instrument work is usually specified in
        # integration time while frame rate is what the display is judged
        # by, and converting in your head each time invites mistakes.
        self.rate_mode = tk.StringVar(value="ms")

        row = ttk.Frame(box)
        row.pack(fill="x")
        ttk.Radiobutton(row, text="Integration (ms)", value="ms",
                        variable=self.rate_mode,
                        command=self._sync_rate_fields).pack(side="left")
        ttk.Radiobutton(row, text="Rate (Hz)", value="hz",
                        variable=self.rate_mode,
                        command=self._sync_rate_fields).pack(side="left",
                                                             padx=(6, 0))

        row1b = ttk.Frame(box)
        row1b.pack(fill="x", pady=(3, 0))
        self.exposure_var = tk.StringVar(value="66.7")
        self.exposure_entry = ttk.Entry(row1b, textvariable=self.exposure_var,
                                        width=9)
        self.exposure_entry.pack(side="left")
        self.exposure_entry.bind("<Return>", lambda e: self._apply_exposure())
        ttk.Button(row1b, text="Set", width=6,
                   command=self._apply_exposure).pack(side="left", padx=4)
        self.exposure_label = ttk.Label(row1b, text="", font=("Consolas", 8))
        self.exposure_label.pack(side="left", padx=(4, 0))

        row2 = ttk.Frame(box)
        row2.pack(fill="x", pady=(4, 0))
        ttk.Label(row2, text="Average").pack(side="left")
        self.avg_var = tk.StringVar(value="1")
        avg = ttk.Combobox(row2, textvariable=self.avg_var, width=6,
                           state="readonly",
                           values=("1", "2", "4", "8", "16", "32", "64"))
        avg.pack(side="left", padx=4)
        avg.bind("<<ComboboxSelected>>", lambda e: self._apply_average())
        ttk.Button(row2, text="Reset", width=6,
                   command=lambda: self.accum.clear()).pack(side="left")

        row3 = ttk.Frame(box)
        row3.pack(fill="x", pady=(6, 0))
        ttk.Label(row3, text="Capture N").pack(side="left")
        self.capture_var = tk.StringVar(value="100")
        ttk.Entry(row3, textvariable=self.capture_var, width=6).pack(
            side="left", padx=4)
        ttk.Button(row3, text="Capture", width=9,
                   command=self._start_capture).pack(side="left")

        row4 = ttk.Frame(box)
        row4.pack(fill="x", pady=(4, 0))
        self.freeze_btn = ttk.Button(row4, text="Freeze", width=9,
                                     command=self._toggle_freeze)
        self.freeze_btn.pack(side="left")
        ttk.Button(row4, text="Save spectrum", width=14,
                   command=self._save_spectrum).pack(side="left", padx=3)

        self.capture_label = ttk.Label(box, text="", font=("Consolas", 8))
        self.capture_label.pack(fill="x", pady=(4, 0))

    def _build_correction(self, parent):
        box = ttk.Labelframe(parent, text="Reference correction", padding=6)
        box.pack(fill="x", pady=(0, 6))

        self.use_dark = tk.BooleanVar(value=False)
        self.use_flat = tk.BooleanVar(value=False)
        # On by default: the end pixels are measured artifacts, not data.
        # See processing.mask_end_pixels.
        self.mask_ends = tk.BooleanVar(value=True)
        # On by default: this removes a measured, non-physical 17.68 kHz
        # component that otherwise contributes most of the apparent noise.
        # See processing.notch_pattern and ISSUES.md entry 31.
        self.use_notch = tk.BooleanVar(value=True)
        ttk.Checkbutton(box, text="Subtract dark", variable=self.use_dark,
                        command=self._redraw).pack(anchor="w")
        ttk.Checkbutton(box, text="Apply flat field", variable=self.use_flat,
                        command=self._redraw).pack(anchor="w")
        ttk.Checkbutton(box, text="Mask end pixels (0, 1023)",
                        variable=self.mask_ends,
                        command=self._redraw).pack(anchor="w")
        ttk.Checkbutton(box, text="Notch 17.68 kHz pattern noise",
                        variable=self.use_notch,
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
                                   font=("Consolas", 8), justify="left")
        self.ref_label.pack(fill="x", pady=(5, 0))

    def _build_readout(self, parent):
        box = ttk.Labelframe(parent, text="Statistics", padding=6)
        box.pack(fill="x")
        self.stat_label = ttk.Label(box, text="no data", font=("Consolas", 9),
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
        self.hover_label = ttk.Label(info_row, text="", font=("Consolas", 9),
                                     anchor="w")
        self.hover_label.pack(side="left", fill="x", expand=True)
        self.zoom_label = ttk.Label(info_row, text="", font=("Consolas", 9),
                                    foreground=FG_DIM, anchor="e")
        self.zoom_label.pack(side="right")
        self.home_btn = ttk.Button(info_row, text="Home", width=6,
                                   command=self._reset_zoom, state="disabled")
        self.home_btn.pack(side="right", padx=(0, 8))

    def _build_tabs(self, parent):
        nb = ttk.Notebook(parent)
        nb.pack(fill="both", expand=True, pady=(6, 0))

        cal = ttk.Frame(nb, padding=6)
        nb.add(cal, text="Calibration")
        self._build_cal_tab(cal)

        dist = ttk.Frame(nb, padding=6)
        nb.add(dist, text="Distribution")
        self._build_dist_tab(dist)

        peaks = ttk.Frame(nb, padding=6)
        nb.add(peaks, text="Peaks")
        self.peak_text = tk.Text(peaks, height=9, font=("Consolas", 9),
                                 bg=PANEL, fg=FG, wrap="none")
        self.peak_text.pack(fill="both", expand=True)
        prow = ttk.Frame(peaks)
        prow.pack(fill="x", pady=(4, 0))
        ttk.Label(prow, text="Min prominence (V)").pack(side="left")
        self.prom_var = tk.StringVar(value="0.005")
        ttk.Entry(prow, textvariable=self.prom_var, width=8).pack(side="left",
                                                                  padx=4)

        diag = ttk.Frame(nb, padding=6)
        nb.add(diag, text="Diagnostics")
        self.diag_text = tk.Text(diag, height=9, font=("Consolas", 8),
                                 bg=PANEL, fg=FG, wrap="none")
        self.diag_text.pack(fill="both", expand=True)
        drow = ttk.Frame(diag)
        drow.pack(fill="x", pady=(4, 0))
        ttk.Button(drow, text="Copy", width=8,
                   command=self._copy_diag).pack(side="left")
        ttk.Button(drow, text="Clear", width=8,
                   command=lambda: self.diag_text.delete("1.0", "end")).pack(
                       side="left", padx=3)
        ttk.Button(drow, text="What do these mean?", width=22,
                   command=self._show_legend).pack(side="left", padx=3)
        self.diag_summary = ttk.Label(diag, text="", font=("Consolas", 8),
                                      justify="left")
        self.diag_summary.pack(fill="x", pady=(4, 0))

    def _build_dist_tab(self, parent):
        top = ttk.Frame(parent)
        top.pack(fill="x")
        ttk.Label(top, text="Click a pixel on the plot for the histogram "
                  "and per-pixel spectrum.").pack(side="left")
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
                            ("Spectrum: per-pixel (time)", "spectrum_time"),
                            ("Spectrum: per-frame (pixels, kHz)", "spectrum_space")):
            ttk.Radiobutton(view_row, text=label, value=val,
                            variable=self.hist_view,
                            command=self._update_pixel_hist).pack(
                                side="left", padx=(0 if val == "hist" else 10, 0))

        self.hist_label = ttk.Label(parent, text="no pixel selected",
                                    font=("Consolas", 9), justify="left")
        self.hist_label.pack(fill="x", pady=(6, 2))

        self.hist_canvas = tk.Canvas(parent, width=HIST_W, height=HIST_H,
                                     bg=BG, highlightthickness=0)
        self.hist_canvas.pack(fill="both", expand=True)

    def _build_cal_tab(self, parent):
        top = ttk.Frame(parent)
        top.pack(fill="x")
        ttk.Label(top, text="Click the plot to set pixel, enter wavelength:"
                  ).pack(side="left")
        self.cal_px = tk.StringVar()
        self.cal_wl = tk.StringVar()
        ttk.Entry(top, textvariable=self.cal_px, width=8).pack(side="left",
                                                               padx=4)
        ttk.Label(top, text="nm").pack(side="left")
        ttk.Entry(top, textvariable=self.cal_wl, width=10).pack(side="left",
                                                                padx=4)
        ttk.Button(top, text="Add", width=6,
                   command=self._add_cal_point).pack(side="left")
        ttk.Button(top, text="Remove", width=8,
                   command=self._remove_cal_point).pack(side="left", padx=3)
        ttk.Button(top, text="Clear all", width=9,
                   command=self._clear_cal).pack(side="left")

        self.cal_text = tk.Text(parent, height=8, font=("Consolas", 9),
                                bg=PANEL, fg=FG, wrap="none")
        self.cal_text.pack(fill="both", expand=True, pady=(6, 0))
        self._refresh_cal()

    # -------------------------------------------------------- connection

    def _refresh_ports(self):
        devices = find_devices()
        values = [f"{d}  {desc}"[:40] for d, desc in devices]
        self.port_cb["values"] = values
        if values and not self.port_var.get():
            self.port_var.set(values[0])
        self._ports = [d for d, _ in devices]

    def _selected_port(self):
        sel = self.port_var.get()
        if not sel:
            return None
        return sel.split()[0]

    def _toggle_connection(self):
        if self.dev.connected:
            self.dev.disconnect()
            self.connect_btn.config(text="Connect")
            self.dev_label.config(text="no device")
            self._log("disconnected")
            return

        port = self._selected_port()
        if not port:
            messagebox.showwarning(APP_NAME, "No serial port selected.")
            return
        try:
            info = self.dev.connect(port)
        except DeviceError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return

        self.connect_btn.config(text="Disconnect")
        self.pixels = info.pixels
        self.accum = processing.Accumulator(self.pixels,
                                            int(self.avg_var.get()))
        # A different device may have a different pixel count, so a zoom
        # expressed in pixel indices from the last one is not meaningful,
        # and neither is a pixel's value history.
        self._reset_zoom()
        self._clear_pixel_history()
        if info.frame_us:
            if self.rate_mode.get() == "ms":
                self.exposure_var.set(f"{info.frame_us / 1000.0:.4g}")
            else:
                self.exposure_var.set(f"{1e6 / info.frame_us:.4g}")
            self._update_exposure_label()
        self._update_device_label()
        self.settings.set("port", port)
        self.settings.save()
        self._log(f"connected to {port}")

    def _update_device_label(self):
        i = self.dev.info
        if i.firmware == "unknown":
            self.dev_label.config(text="connected, device did not identify")
            return
        # Step size is the smallest distinguishable change: one code.
        # Reported in microvolts because at 14 bits over 2.5 V it is well
        # under a millivolt, and it is the floor against which any noise
        # figure should be read.
        lsb_uv = (i.vref_mv * 1000.0) / i.full_scale
        self.dev_label.config(
            text=(f"{i.board}  fw {i.firmware}\n"
                  f"{i.pixels} px  {i.bits}-bit  {i.vref_mv} mV full scale\n"
                  f"step {lsb_uv:.2f} uV/code ({i.full_scale} codes)\n"
                  f"sysclk {i.sysclk_mhz} MHz  built {i.built}\n"
                  f"pio {i.pio_crc}"))

    def _sync_rate_fields(self):
        """Convert the entry between units when the mode changes, so the
        displayed number always means what the selected unit says."""
        try:
            value = float(self.exposure_var.get())
        except ValueError:
            return
        if value <= 0:
            return
        # The field held the other unit until this moment.
        converted = 1000.0 / value
        self.exposure_var.set(f"{converted:.4g}")
        self._update_exposure_label()

    def _current_us(self):
        try:
            value = float(self.exposure_var.get())
        except ValueError:
            return None
        if value <= 0:
            return None
        if self.rate_mode.get() == "ms":
            return value * 1000.0
        return 1000000.0 / value

    def _update_exposure_label(self):
        us = self._current_us()
        if us is None:
            self.exposure_label.config(text="")
            return
        # Always show the equivalent in the other unit, so the two are
        # never out of step in the operator's head.
        if self.rate_mode.get() == "ms":
            self.exposure_label.config(text=f"= {1e6 / us:.2f} Hz")
        else:
            self.exposure_label.config(text=f"= {us / 1000.0:.2f} ms")

    def _apply_exposure(self):
        if not self.dev.connected:
            return
        us = self._current_us()
        if us is None:
            return
        lo = self.dev.info.frame_us_min or 20600
        hi = self.dev.info.frame_us_max or 10000000
        if us < lo:
            messagebox.showwarning(
                APP_NAME,
                f"Integration time cannot be shorter than the scan.\n\n"
                f"Requested {us/1000:.2f} ms, minimum {lo/1000:.2f} ms "
                f"({1e6/lo:.1f} Hz).")
            us = lo
        us = max(lo, min(hi, us))
        try:
            self.dev.set_integration_us(us)
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
        if not self.dev.connected:
            messagebox.showwarning(APP_NAME, "Connect to a device first.")
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
            self.freeze_btn.config(text="Unfreeze")
            self._log(f"captured {len(frames)} frames in {elapsed:.1f} s")

        self._update_ref_label()
        self.capture_label.config(text="")

    def _toggle_freeze(self):
        if self.frozen is not None:
            self.frozen = None
            self.freeze_btn.config(text="Freeze")
        elif self.display is not None:
            self.frozen = self.display.copy()
            self.freeze_btn.config(text="Unfreeze")
        self._redraw()

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
        i = self.dev.info
        v = processing.codes_to_volts(frame.codes, i.vref_mv, i.full_scale)
        # notch_pattern guards the end pixels itself, so this is
        # independent of the mask setting and of the order of the two.
        if self.use_notch.get():
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
        for kind, text in self.dev.drain_messages():
            if kind == "info":
                self._update_device_label()
            self._log(text if kind != "text" else text, tag=kind)

        stats = self.dev.drain_stats()
        if stats:
            self.last_stats = stats[-1]
            for s in stats[-6:]:
                self._log(s.text, tag="stat")
            self._update_diag_summary()

        frames = self.dev.drain_frames()
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
                f.codes, self.dev.info.vref_mv, self.dev.info.full_scale))
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
        i = self.dev.info
        full = i.vref_mv / 1000.0
        finite = volts[np.isfinite(volts)]
        hi = float(np.max(finite)) if finite.size else full
        if hi <= 0:
            hi = full
        cells = []
        for v in volts:
            if not np.isfinite(v):
                cells.append(MISSING)
            else:
                g = int(max(0.0, min(1.0, v / hi)) * 255)
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
        full = self.dev.info.vref_mv / 1000.0
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
                          fill=AXIS, anchor="e", font=("Consolas", 8))
        c.create_text(MARGIN_L - 6, MARGIN_T - 2, text="mV", fill=AXIS,
                      anchor="se", font=("Consolas", 8))

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
            c.create_text(x, MARGIN_T + h + 11, text=label, fill=AXIS,
                          font=("Consolas", 8))
        # Unit centred beneath the plot rather than at the right edge,
        # where it previously overlapped the final tick label.
        c.create_text(MARGIN_L + PLOT_W // 2, MARGIN_T + h + 22,
                      text="wavelength (nm)" if wl is not None else "pixel",
                      fill=AXIS, font=("Consolas", 8))

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
                          font=("Consolas", 8))

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
        lsb_mv = self.dev.info.vref_mv / self.dev.info.full_scale
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
        if self.dev.connected:
            parts.append(f"{self.dev.port}")
            parts.append(f"{self.fps:5.2f} fps")
            parts.append(f"{self.dev.frames_received} frames")
        else:
            parts.append("Disconnected")
        if self.last_stats is not None:
            s = self.last_stats
            parts.append(f"miss {s.miss}")
            parts.append(f"period {s.period_us}us")
            if s.wdog or s.nostart:
                parts.append("FAULT")
        if self.frozen is not None:
            parts.append("FROZEN")
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
        frame_us = getattr(self.dev.info, "frame_us", None)
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
            self.hist_label.config(text="no pixel selected")
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
            c.create_text(x, mt + plot_h + 4, text=f"{v:g}", fill=AXIS,
                          anchor="n", font=("Consolas", 8))
        c.create_text(ml + plot_w // 2, mt + plot_h + 15, text="mV",
                      fill=AXIS, font=("Consolas", 8))

        for i in range(bins):
            bx0, bx1 = x_of(edges[i]), x_of(edges[i + 1])
            by0 = y_of(counts[i])
            c.create_rectangle(bx0, by0, bx1, mt + plot_h,
                               fill=TRACE, outline=BG)

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

        lsb_mv = (self.dev.info.vref_mv / self.dev.info.full_scale
                  if self.dev.connected else None)
        text = f"{name}   n={n}   mean={mean:.3f} mV   std={std:.4f} mV"
        if lsb_mv:
            text += f"  ({std/lsb_mv:.1f} codes)"
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
            c.create_text(x, mt + plot_h + 4, text=self._fmt_hz(f),
                          fill=AXIS, anchor=anchor, font=("Consolas", 8))
        c.create_text(ml + plot_w // 2, mt + plot_h + 15, text="frequency",
                      fill=AXIS, font=("Consolas", 8))

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
                          font=("Consolas", 8))
        c.create_text(ml - 4, mt - 4, text="dB/mV", fill=AXIS, anchor="se",
                      font=("Consolas", 8))

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
                      anchor=anchor, font=("Consolas", 8))

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

        pixel_period_us = getattr(self.dev.info, "pixel_period_us", 0) or 20.0
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

    def _refresh_cal(self):
        self.cal_text.delete("1.0", "end")
        self.cal_text.insert("end", self.cal.describe() + "\n\n")
        if not self.cal.points:
            self.cal_text.insert(
                "end",
                "Add at least two reference points from a source with known\n"
                "emission lines. Click the plot to fill the pixel field.\n")
            return
        self.cal_text.insert("end",
                             "   pixel        nm       fit-nm    residual\n")
        for px, wl, res in (self.cal.residuals() or
                            [(p, w, float('nan')) for p, w in self.cal.points]):
            fit = wl + res if np.isfinite(res) else float("nan")
            self.cal_text.insert(
                "end", f"{px:8.2f} {wl:10.3f} {fit:10.3f} {res:10.3f}\n")

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
        try:
            export.write_spectrum(
                path, np.arange(len(data)), data, wavelengths=wl,
                info=self.dev.info, frames=max(1, self.accum.depth),
                elapsed_s=self.accum.depth / self.fps if self.fps else 0.0,
                dark_applied=self.use_dark.get() and self.dark is not None,
                flat_applied=self.use_flat.get() and self.flat is not None,
                notch_applied=self.use_notch.get(),
                calibration=self.cal, peaks=self.peaks)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Save failed: {exc}")
            return
        self._log(f"saved {Path(path).name}")

    # --------------------------------------------------------- diagnostics

    def _log(self, text, tag=None):
        if not hasattr(self, "diag_text"):
            return
        at_end = self.diag_text.yview()[1] > 0.995
        self.diag_text.insert("end", text + "\n")
        if int(self.diag_text.index("end-1c").split(".")[0]) > 2000:
            self.diag_text.delete("1.0", "500.0")
        if at_end:
            self.diag_text.see("end")

    def _show_legend(self):
        win = tk.Toplevel(self.root)
        win.title("Diagnostic fields")
        win.configure(bg=BG)
        win.geometry("760x560")
        txt = tk.Text(win, font=("Consolas", 9), bg=PANEL, fg=FG,
                      wrap="word", padx=8, pady=8)
        scroll = ttk.Scrollbar(win, command=txt.yview)
        txt.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        txt.pack(fill="both", expand=True)

        txt.insert("end", "Per-frame diagnostics\n\n")
        txt.insert("end",
                   "One line per frame. Healthy values are given so a "
                   "problem is visible without needing to remember what "
                   "normal looks like.\n\n")
        for name, healthy, meaning in DIAGNOSTIC_LEGEND:
            txt.insert("end", f"{name}", "field")
            txt.insert("end", f"   healthy: {healthy}\n", "healthy")
            txt.insert("end", f"    {meaning}\n\n")
        txt.tag_config("field", font=("Consolas", 10, "bold"))
        txt.tag_config("healthy", foreground=FG_DIM)
        txt.configure(state="disabled")

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
                  f"reread {s.reread}   txdrop {s.txdrop}"))

    def on_close(self):
        self._persist_cal()
        self.dev.disconnect()
        self.root.destroy()


def main():
    root = tk.Tk()
    app = SpectrometerApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
