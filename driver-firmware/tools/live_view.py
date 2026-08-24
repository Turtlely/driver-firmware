#!/usr/bin/env python3
"""Live view and diagnostics monitor for the Hamamatsu S3903-1024Q line sensor.

The firmware streams two line types per frame over USB serial (see
runFrame() in src/main.cpp).

Frame data:

    D <frame_seq> <captured_count> <4096 hex chars>

Each group of 4 uppercase hex digits is one pixel slot. A genuine pixel
is a 14-bit ADC code (0x0000-0x3FFF), full scale corresponding to
VREF = 2.5 V (REF5025). The value FFFF is the MISSING_PIXEL sentinel,
meaning no trigger arrived for that slot; it is outside the 14-bit range
so a real conversion can never produce it.

Missing pixels may appear ANYWHERE in a frame, not only as a trailing
run, because firmware indexes each pixel by arrival time rather than
arrival order. A lost pixel therefore leaves a hole in place instead of
shifting every later pixel, which is what previously made the image
appear to slide sideways.

Frame diagnostics:

    S <frame_seq> pulses <n> raw <n> miss <n> period <n>us wdog <0|1>
      early <n> fifo <n> gap <min>-<max>us late <n>us dur <n>us
      maxread <n>us txdrop <n> px<idx> <code> (0x<hex>, <mV> mV)

This viewer shows three synchronized views of the newest frame plus a
scrolling diagnostics log:

  - a 1024-pixel black/white strip (black = 0 V, white = 2.5 V)
  - a line graph of the same data in millivolts
  - a diagnostics panel with the raw S lines and aggregate statistics

The diagnostics panel exists because a COM port on Windows is owned
exclusively by one process. Running this viewer meant no terminal could
be attached to read the S lines, so the timing diagnostics the firmware
emits were effectively unreadable while the viewer was in use. "Copy All"
puts the entire session log on the clipboard for pasting into a report or
a bug thread.

Usage:
    python live_view.py [PORT] [BAUD]

If PORT is omitted, the first Raspberry Pi Pico (VID 0x2E8A) is used.
BAUD defaults to 115200 (USB CDC mostly ignores this anyway).

Requires: pip install pyserial
tkinter ships with the standard python.org Windows installer.
"""

import math
import queue
import re
import sys
import threading
import time
import tkinter as tk
from collections import deque
from dataclasses import dataclass, field

import serial
from serial.tools import list_ports

PIXELS = 1024
FULL_SCALE = 1 << 14  # 14-bit codes
VREF_MV = 2500
HEX_LEN = PIXELS * 4
MAX_CODE = FULL_SCALE - 1  # 0x3FFF

# Firmware writes this into any pixel slot whose trigger never arrived
# (MISSING_PIXEL in src/main.cpp). Deliberately outside the 14-bit code
# range so it is unambiguous against a genuine 0 mV reading.
MISSING_PIXEL = 0xFFFF

STRIP_HEIGHT = 60  # rendered pixels tall, purely for visibility
GRAPH_HEIGHT = 240
GRAPH_MARGIN_LEFT = 48  # room for mV axis labels
GRAPH_MARGIN_TOP = 10
GRAPH_MARGIN_BOTTOM = 10
LOG_ROWS = 12
DEFAULT_BAUD = 115200
UI_POLL_MS = 33  # ~30 Hz UI refresh; actual frame rate is link-limited well below this
LOG_CAPACITY = 5000  # S/boot lines retained for "Copy All"

# --- Spike analysis ---------------------------------------------------
# A pixel whose value differs sharply from BOTH neighbours is not something
# the optics can produce: a real feature is blurred across several pixels by
# the point spread function, so it appears as a step or a broad bump, never
# as a single isolated sample. An isolated outlier is therefore electrical,
# and its amplitude says which layer it came from.
#
# A single misread bit in the SPI transfer perturbs the code by exactly a
# power of two. Analog noise, reference droop, or incomplete settling
# perturb it by an arbitrary amount. Measuring how tightly the outlier
# amplitudes cluster on powers of two separates those two causes without
# any guesswork, and identifies the specific bit if it is the former.
SPIKE_THRESHOLD = 400        # codes; deviation from the neighbour average to count as an outlier
SPIKE_MIN_BIT = 5            # ignore very small powers of two; they are indistinguishable from noise

# How close to an exact power of two an amplitude must be to be attributed
# to a bit. This has to be tight. A genuine single-bit error lands on 2^k
# exactly, give or take the small error in estimating the true value from
# the two neighbours, so a narrow band loses almost nothing. A wide band,
# by contrast, catches arbitrary amplitudes by luck: powers of two are
# spaced one octave apart, so a band of +/-18 percent covers over half an
# octave and would flag random analog noise as bit errors about half the
# time. Measured against simulated analog noise, 18 percent produced 39
# percent false attribution; 6 percent brings the chance rate to the value
# reported as POW2_CHANCE_RATE below, which the verdict compares against
# explicitly rather than assuming.
POW2_TOLERANCE = 0.06

# Fraction of an octave the tolerance band covers, which is the rate at
# which amplitudes of arbitrary (non-bit-error) origin land in it purely by
# chance. The verdict is meaningful only relative to this baseline.
POW2_CHANCE_RATE = math.log2((1.0 + POW2_TOLERANCE) / (1.0 - POW2_TOLERANCE))


def implied_bit(delta):
    """Return the bit index k if |delta| is close to 2**k, else None."""
    m = abs(delta)
    if m < (1 << SPIKE_MIN_BIT):
        return None
    k = int(round(math.log2(m)))
    exact = 1 << k
    if abs(m - exact) <= exact * POW2_TOLERANCE:
        return k
    return None


# Tolerance on the 2x / 0.5x ratio test for detecting a shifted read.
SHIFT_RATIO_TOLERANCE = 0.08

# Half-width of the window used for the robust local baseline. Wide enough
# that a median over it survives one or two corrupt samples inside it.
SPIKE_WINDOW = 3


def analyze_spikes(codes):
    """Find isolated single-pixel outliers and classify what produced them.

    Distinguishes three mechanisms, which is the whole point:

    - A shifted read. The SPI transfer gained or lost one clock relative
      to the data stream, so the value comes back as exactly the true
      value shifted one bit, meaning 2x or 0.5x. This was the actual
      fault found on this hardware.
    - A flipped bit. A single bit was misread, changing the value by a
      fixed power of two.
    - Neither, which points at an analog cause.

    Testing only for flips is not sufficient and actively misleads. A
    shift scales the error with the signal rather than fixing it at 2^k,
    so a flip-only test classifies a shifted read as analog noise. That
    mistake was made here and cost real debugging time before the raw
    values were inspected directly.

    Returns (spikes, kinds) where spikes is a list of (index, delta) and
    kinds is a dict with counts for 'shift_left', 'shift_right', 'other',
    plus per-bit counts under 'bits'."""
    spikes = []
    kinds = {"shift_left": 0, "shift_right": 0, "other": 0, "bits": {}}

    for i in range(SPIKE_WINDOW, PIXELS - SPIKE_WINDOW):
        b = codes[i]
        if b > MAX_CODE:
            continue  # a hole; nothing to compare against

        # Baseline is the MEDIAN of the surrounding pixels, excluding the
        # pixel under test. A plain average of the two immediate neighbours
        # is not usable here: a corrupt pixel poisons the baseline of both
        # of ITS neighbours, so every real fault produced three detections,
        # the two spurious ones landing in whichever category happened to
        # fit. The median over a wider window shrugs off a corrupt sample
        # inside it, so each fault is counted once and classified against a
        # clean reference.
        window = [codes[j]
                  for j in range(i - SPIKE_WINDOW, i + SPIKE_WINDOW + 1)
                  if j != i and codes[j] <= MAX_CODE]
        if len(window) < SPIKE_WINDOW:
            continue
        window.sort()
        expected = window[len(window) // 2]

        delta = b - expected
        if abs(delta) < SPIKE_THRESHOLD:
            continue
        spikes.append((i, delta))

        # Shift test first: it is the more specific hypothesis, and a
        # shifted value would otherwise fall through and be misread as
        # analog.
        if expected > 0:
            ratio = b / expected
            if abs(ratio - 2.0) <= 2.0 * SHIFT_RATIO_TOLERANCE:
                kinds["shift_left"] += 1
                continue
            if abs(ratio - 0.5) <= 0.5 * SHIFT_RATIO_TOLERANCE:
                kinds["shift_right"] += 1
                continue

        k = implied_bit(delta)
        if k is None:
            kinds["other"] += 1
        else:
            kinds["bits"][k] = kinds["bits"].get(k, 0) + 1

    return spikes, kinds


COLOR_BG = "#111111"
COLOR_GRID = "#333333"
COLOR_TRACE = "#39ff14"  # real, captured pixels
COLOR_INCOMPLETE = "#ff3b30"  # MISSING_PIXEL slots
COLOR_WARN = "#ffb020"
COLOR_SPIKE = "#00d0ff"  # isolated value outliers (see analyze_spikes)


@dataclass
class Frame:
    codes: list  # 1024 pixel slots: 14-bit ADC codes, or MISSING_PIXEL for holes
    captured: int  # how many real pixels this frame received
    seq: int  # firmware frame counter, monotonically increasing


@dataclass
class Stats:
    """Aggregates that answer the two standing questions directly: is the
    capture path still losing pixels, and are whole frames going missing."""

    frames_seen: int = 0
    first_seq: int = None
    last_seq: int = None
    seq_gap_frames: int = 0  # frames the firmware numbered but we never received
    last_txdrop: int = None
    txdrop_frames: int = 0  # of those, ones firmware deliberately skipped sending
    frames_with_missing: int = 0
    total_missing: int = 0
    worst_missing: int = 0
    wdog_frames: int = 0
    nostart_frames: int = 0  # CPLD ignored START and the retries never took
    start_retries: int = 0   # extra START pulses issued; recovered frames
    total_early: int = 0
    max_fifo: int = 0
    max_late_us: int = 0
    periods: list = field(default_factory=list)
    parse_errors: int = 0

    # Spike analysis, accumulated over rendered frames
    spike_frames: int = 0
    total_spikes: int = 0
    worst_spikes: int = 0
    bit_hits: dict = field(default_factory=dict)
    shift_left: int = 0
    shift_right: int = 0
    non_pow2: int = 0
    rendered: int = 0


def find_port() -> str:
    for p in list_ports.comports():
        if p.vid == 0x2E8A:  # Raspberry Pi
            return p.device
    ports = list(list_ports.comports())
    if not ports:
        sys.exit("No serial ports found. Pass one explicitly: python live_view.py COM5")
    return ports[0].device


def _num(pattern, text, default=None):
    m = re.search(pattern, text)
    return int(m.group(1)) if m else default


def parse_s_line(text):
    """Pull the numeric fields out of an S line. Returns a dict, or None if
    the line does not look like an S line at all."""
    if not text.startswith("S "):
        return None
    # Firmware pads these with %3lu/%4lu, so "gap  19-  21us" is normal:
    # whitespace can appear on either side of the dash.
    gap = re.search(r"gap\s+(\d+)\s*-\s*(\d+)\s*us", text)
    return {
        "seq": _num(r"^S\s+(\d+)", text),
        "pulses": _num(r"pulses\s+(\d+)", text),
        "raw": _num(r"\braw\s+(\d+)", text),
        "miss": _num(r"\bmiss\s+(\d+)", text),
        "period": _num(r"period\s+(\d+)us", text),
        "wdog": _num(r"wdog\s+(\d+)", text),
        "nostart": _num(r"nostart\s+(\d+)", text),
        "retry": _num(r"retry\s+(\d+)", text),
        "early": _num(r"early\s+(\d+)", text),
        "fifo": _num(r"fifo\s+(\d+)", text),
        "gap_min": int(gap.group(1)) if gap else None,
        "gap_max": int(gap.group(2)) if gap else None,
        "late": _num(r"late\s+(\d+)us", text),
        "dur": _num(r"dur\s+(\d+)us", text),
        "maxread": _num(r"maxread\s+(\d+)us", text),
        "txdrop": _num(r"txdrop\s+(\d+)", text),
    }


class SerialReader(threading.Thread):
    """Reads lines off the serial port on a background thread. Frames go to
    a queue drained by the UI; every non-frame line (S diagnostics, boot
    banner) goes to a separate log queue so nothing the firmware emits is
    discarded. A slow display can never block the port."""

    def __init__(self, port: str, baud: int, frame_q, log_q):
        super().__init__(daemon=True)
        self.frame_q = frame_q
        self.log_q = log_q
        self._stop = threading.Event()
        self.error = None
        try:
            self.ser = serial.Serial(port, baud, timeout=0.2)
        except serial.SerialException as exc:
            self.ser = None
            self.error = str(exc)

    def run(self):
        if self.ser is None:
            return
        while not self._stop.is_set():
            try:
                raw = self.ser.readline()
            except (serial.SerialException, TypeError, OSError, ValueError):
                # SerialException is a genuine port fault. The others show up
                # only on shutdown, when stop() closes the handle while a read
                # is already in flight; pyserial's Windows backend then throws
                # from inside ctypes rather than raising SerialException.
                break
            if not raw:
                continue

            text = raw.decode("ascii", "replace").strip()
            if not text:
                continue

            if text.startswith("D "):
                parts = text.split(" ")
                if len(parts) != 4 or len(parts[3]) != HEX_LEN:
                    self.log_q.put(("bad", f"malformed D line (len {len(text)})"))
                    continue
                try:
                    seq = int(parts[1])
                    captured = int(parts[2])
                    hexs = parts[3]
                    codes = [int(hexs[i:i + 4], 16) for i in range(0, HEX_LEN, 4)]
                except ValueError:
                    self.log_q.put(("bad", "unparseable D line"))
                    continue
                self.frame_q.put(Frame(codes=codes,
                                       captured=min(captured, PIXELS),
                                       seq=seq))
            else:
                # S diagnostics, BOOT banner, anything else the firmware says.
                self.log_q.put(("line", text))

    def stop(self):
        self._stop.set()
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass


class LiveViewer:
    def __init__(self, root: tk.Tk, frame_q, log_q, port: str):
        self.root = root
        self.frame_q = frame_q
        self.log_q = log_q
        self.port = port
        self.frame_count = 0
        self.stats = Stats()
        self.log_lines = deque(maxlen=LOG_CAPACITY)
        self.paused = False
        self.started_at = time.time()

        # --- grayscale strip ---
        # Same width as the graph, and the image is inset by the graph's left
        # margin, so pixel i sits at the same screen x in both views. Without
        # that inset the strip is offset from the trace by GRAPH_MARGIN_LEFT
        # and a feature cannot be read off one against the other.
        self.strip = tk.Canvas(
            root, width=PIXELS + GRAPH_MARGIN_LEFT, height=STRIP_HEIGHT,
            bg=COLOR_BG, highlightthickness=0
        )
        self.strip.pack()
        self.strip_image = tk.PhotoImage(width=PIXELS, height=STRIP_HEIGHT)
        self.strip.create_image(GRAPH_MARGIN_LEFT, 0, anchor="nw",
                                image=self.strip_image)

        # --- line graph ---
        graph_width = PIXELS + GRAPH_MARGIN_LEFT
        self.graph = tk.Canvas(
            root, width=graph_width, height=GRAPH_HEIGHT, bg=COLOR_BG, highlightthickness=0
        )
        self.graph.pack()
        self._draw_graph_axes()
        self.trace_ids = []  # canvas line-segment ids, replaced every frame

        self.status = tk.Label(root, text="waiting for data...", anchor="w",
                               font=("Consolas", 10))
        self.status.pack(fill="x")

        # --- diagnostics panel ---
        self.summary = tk.Label(root, text="", anchor="w", justify="left",
                                font=("Consolas", 9), fg="#dddddd", bg="#1b1b1b")
        self.summary.pack(fill="x")

        log_frame = tk.Frame(root)
        log_frame.pack(fill="both", expand=True)
        scroll = tk.Scrollbar(log_frame)
        scroll.pack(side="right", fill="y")
        self.log = tk.Text(log_frame, height=LOG_ROWS, font=("Consolas", 9),
                           bg="#0d0d0d", fg="#cccccc", insertbackground="#cccccc",
                           wrap="none", yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.config(command=self.log.yview)
        self.log.tag_config("warn", foreground=COLOR_WARN)
        self.log.tag_config("bad", foreground=COLOR_INCOMPLETE)
        self.log.tag_config("boot", foreground="#6fc3ff")

        btns = tk.Frame(root)
        btns.pack(fill="x")
        self.copy_btn = tk.Button(btns, text="Copy All", width=12,
                                  command=self.copy_all)
        self.copy_btn.pack(side="left", padx=2, pady=2)
        tk.Button(btns, text="Copy Summary", width=14,
                  command=self.copy_summary).pack(side="left", padx=2, pady=2)
        self.pause_btn = tk.Button(btns, text="Pause Log", width=12,
                                   command=self.toggle_pause)
        self.pause_btn.pack(side="left", padx=2, pady=2)
        tk.Button(btns, text="Clear", width=10,
                  command=self.clear_log).pack(side="left", padx=2, pady=2)
        self.copy_note = tk.Label(btns, text="", anchor="w", font=("Consolas", 9),
                                  fg="#39ff14")
        self.copy_note.pack(side="left", padx=8)

        self._poll()

    # ---------- diagnostics ----------

    def _append_log(self, text, tag=None):
        self.log_lines.append(text)
        if self.paused:
            return
        at_bottom = self.log.yview()[1] > 0.999
        self.log.insert("end", text + "\n", tag or ())
        # Keep the widget bounded; the deque retains full history for copying.
        if int(self.log.index("end-1c").split(".")[0]) > LOG_ROWS * 40:
            self.log.delete("1.0", f"{LOG_ROWS * 10}.0")
        if at_bottom:
            self.log.see("end")

    def _handle_log_line(self, kind, text):
        st = self.stats
        if kind == "bad":
            st.parse_errors += 1
            self._append_log(f"[!] {text}", "bad")
            return

        if text.startswith("BOOT:"):
            self._append_log(text, "boot")
            return

        d = parse_s_line(text)
        if d is None:
            self._append_log(text)
            return

        st.frames_seen += 1
        if st.first_seq is None:
            st.first_seq = d["seq"]

        # Whole-frame loss: firmware numbers frames monotonically, so a jump
        # in seq means frames were emitted that we never received. Comparing
        # against txdrop separates the two possible causes - firmware
        # deliberately skipping a frame whose predecessor had not finished
        # transmitting, versus data lost on the wire.
        if st.last_seq is not None and d["seq"] is not None:
            step = d["seq"] - st.last_seq
            if step > 1:
                lost = step - 1
                st.seq_gap_frames += lost
                dropped_here = 0
                if d["txdrop"] is not None and st.last_txdrop is not None:
                    dropped_here = d["txdrop"] - st.last_txdrop
                    st.txdrop_frames += dropped_here
                cause = ("firmware txdrop (host too slow)" if dropped_here >= lost
                         else "LOST ON WIRE (not txdrop)")
                self._append_log(
                    f"[!] frame gap: seq {st.last_seq} -> {d['seq']} "
                    f"({lost} frame(s) missing, {cause})", "warn")
        if d["seq"] is not None:
            st.last_seq = d["seq"]
        if d["txdrop"] is not None:
            st.last_txdrop = d["txdrop"]

        miss = d["miss"] or 0
        if miss:
            st.frames_with_missing += 1
            st.total_missing += miss
            st.worst_missing = max(st.worst_missing, miss)
        if d["wdog"]:
            st.wdog_frames += 1
        if d["nostart"]:
            st.nostart_frames += 1
        if d["retry"]:
            st.start_retries += d["retry"]
        st.total_early += d["early"] or 0
        if d["fifo"] is not None:
            st.max_fifo = max(st.max_fifo, d["fifo"])
        if d["late"] is not None:
            st.max_late_us = max(st.max_late_us, d["late"])
        if d["period"]:
            st.periods.append(d["period"])
            if len(st.periods) > 500:
                del st.periods[:250]

        tag = "warn" if (miss or d["wdog"] or d["nostart"] or d["retry"]) else None
        self._append_log(text, tag)

    def _summary_text(self):
        st = self.stats
        elapsed = max(time.time() - self.started_at, 0.001)
        fps = st.frames_seen / elapsed
        emitted = ((st.last_seq - st.first_seq + 1)
                   if (st.last_seq is not None and st.first_seq is not None) else 0)
        wire_lost = st.seq_gap_frames - st.txdrop_frames
        period = (sum(st.periods) / len(st.periods)) if st.periods else 0
        avg_miss = (st.total_missing / st.frames_seen) if st.frames_seen else 0
        return (
            f" port {self.port}   {elapsed:6.1f}s   {fps:5.2f} fps received\n"
            f" frames: {st.frames_seen} received / {emitted} emitted by firmware"
            f"   |  MISSING FRAMES: {st.seq_gap_frames}"
            f"  (txdrop {st.txdrop_frames}, wire {wire_lost})\n"
            f" pixels: frames with holes {st.frames_with_missing}/{st.frames_seen}"
            f"   avg miss/frame {avg_miss:5.2f}   worst {st.worst_missing}"
            f"   total {st.total_missing}\n"
            f" timing: period {period:5.1f}us   max fifo {st.max_fifo}/8"
            f"   max late {st.max_late_us}us   wdog {st.wdog_frames}"
            f"   early {st.total_early}   parse err {st.parse_errors}\n"
            f" START : ignored-by-CPLD {st.nostart_frames}"
            f"   retries issued {st.start_retries}"
            f"   (retries that recovered = retries issued - ignored)\n"
            f" SPIKES: {self._spike_text()}"
        )

    def _spike_text(self):
        """Summarize isolated value outliers and what their amplitudes imply.

        Amplitudes clustered on powers of two indicate single-bit SPI read
        errors, and name the offending bit. Amplitudes that are mostly not
        powers of two indicate an analog cause instead. See analyze_spikes."""
        st = self.stats
        if st.rendered == 0:
            return "no frames rendered yet"
        if st.total_spikes == 0:
            return f"none in {st.rendered} frames rendered"

        per_frame = st.total_spikes / st.rendered
        shifts = st.shift_left + st.shift_right
        pow2_total = sum(st.bit_hits.values())
        total = shifts + pow2_total + st.non_pow2
        if total == 0:
            return f"none in {st.rendered} frames rendered"

        shift_frac = shifts / total
        pow2_frac = pow2_total / total
        chance = POW2_CHANCE_RATE

        top = sorted(st.bit_hits.items(), key=lambda kv: -kv[1])[:3]
        bits = "  ".join(f"bit{k}={v}" for k, v in top) or "-"

        # Shifts are checked first: they are the more specific hypothesis,
        # and a shifted read misclassifies as analog under a flip-only test.
        if total < 20:
            verdict = "-> too few samples to judge"
        elif shift_frac >= 0.5:
            verdict = "-> SHIFTED READS: SPI gained/lost a clock (check CS setup time)"
        elif pow2_frac >= 3.0 * chance:
            verdict = "-> FLIPPED BITS: single-bit SPI read errors"
        elif shift_frac <= 0.10 and pow2_frac <= 1.5 * chance:
            verdict = "-> ANALOG: settling / droop / noise"
        else:
            verdict = "-> mixed, inconclusive"

        return (f"{st.total_spikes} total, {per_frame:.2f}/frame, worst {st.worst_spikes}\n"
                f"         shift<<1 {st.shift_left}  shift>>1 {st.shift_right}"
                f"  ({100*shift_frac:.0f}%)   flips {pow2_total} [{bits}]"
                f"   other {st.non_pow2}\n"
                f"         {verdict}")

    # ---------- buttons ----------

    def _flash(self, msg):
        self.copy_note.config(text=msg)
        self.root.after(2500, lambda: self.copy_note.config(text=""))

    def _report_header(self):
        return [
            "=== driver-firmware capture report ===",
            f"port: {self.port}",
            f"captured: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            self._summary_text(),
            "",
        ]

    def copy_all(self):
        payload = "\n".join(self._report_header() + list(self.log_lines))
        self.root.clipboard_clear()
        self.root.clipboard_append(payload)
        self.root.update()  # ensure the clipboard survives this process exiting
        self._flash(f"copied {len(self.log_lines)} lines")

    def copy_summary(self):
        payload = "\n".join(self._report_header())
        self.root.clipboard_clear()
        self.root.clipboard_append(payload)
        self.root.update()
        self._flash("summary copied")

    def toggle_pause(self):
        self.paused = not self.paused
        self.pause_btn.config(text="Resume Log" if self.paused else "Pause Log")

    def clear_log(self):
        self.log_lines.clear()
        self.log.delete("1.0", "end")
        self.stats = Stats()
        self.started_at = time.time()

    # ---------- rendering ----------

    def _draw_graph_axes(self):
        plot_h = GRAPH_HEIGHT - GRAPH_MARGIN_TOP - GRAPH_MARGIN_BOTTOM
        for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
            y = GRAPH_MARGIN_TOP + plot_h * (1 - frac)
            mv = round(frac * VREF_MV)
            self.graph.create_line(
                GRAPH_MARGIN_LEFT, y, GRAPH_MARGIN_LEFT + PIXELS, y, fill=COLOR_GRID
            )
            self.graph.create_text(
                GRAPH_MARGIN_LEFT - 6, y, text=f"{mv}", fill="#888888",
                anchor="e", font=("Consolas", 8)
            )

    def _poll(self):
        # Drain diagnostics first so a frame's stats are logged alongside it.
        drained = 0
        try:
            while drained < 400:
                kind, text = self.log_q.get_nowait()
                self._handle_log_line(kind, text)
                drained += 1
        except queue.Empty:
            pass

        frame = None
        try:
            while True:
                frame = self.frame_q.get_nowait()  # keep draining to the newest frame
        except queue.Empty:
            pass

        if frame is not None:
            self._render(frame)
        if drained or frame is not None:
            self.summary.config(text=self._summary_text())

        self.root.after(UI_POLL_MS, self._poll)

    def _render(self, frame: Frame):
        self.frame_count += 1
        codes = frame.codes
        captured = frame.captured
        missing = [i for i, c in enumerate(codes) if c > MAX_CODE]

        spikes, kinds = analyze_spikes(codes)
        st = self.stats
        st.rendered += 1
        if spikes:
            st.spike_frames += 1
            st.total_spikes += len(spikes)
            st.worst_spikes = max(st.worst_spikes, len(spikes))
        st.shift_left += kinds["shift_left"]
        st.shift_right += kinds["shift_right"]
        st.non_pow2 += kinds["other"]
        for k, v in kinds["bits"].items():
            st.bit_hits[k] = st.bit_hits.get(k, 0) + v

        # --- strip ---
        # Holes render red so they are visibly distinct from a genuine dark
        # reading, which the previous zero-fill convention could not express.
        cells = []
        for c in codes:
            if c > MAX_CODE:
                cells.append(COLOR_INCOMPLETE)
            else:
                g = self._gray(c)
                cells.append(f"#{g:02x}{g:02x}{g:02x}")
        row = "{" + " ".join(cells) + "}"
        self.strip_image.put(" ".join([row] * STRIP_HEIGHT))

        # --- graph ---
        for item_id in self.trace_ids:
            self.graph.delete(item_id)
        self.trace_ids = []

        plot_h = GRAPH_HEIGHT - GRAPH_MARGIN_TOP - GRAPH_MARGIN_BOTTOM

        def to_xy(i, code):
            x = GRAPH_MARGIN_LEFT + i
            mv = self._mv(code)
            y = GRAPH_MARGIN_TOP + plot_h * (1 - mv / VREF_MV)
            return x, y

        # Plot runs of consecutive present pixels, breaking the trace at each
        # hole rather than interpolating across it. Interpolation would draw
        # data that was never measured.
        run = []
        for i, code in enumerate(codes):
            if code > MAX_CODE:
                if len(run) >= 2:
                    self.trace_ids.append(self.graph.create_line(
                        *[v for p in run for v in p], fill=COLOR_TRACE, width=1))
                run = []
            else:
                run.append(to_xy(i, code))
        if len(run) >= 2:
            self.trace_ids.append(self.graph.create_line(
                *[v for p in run for v in p], fill=COLOR_TRACE, width=1))

        # Full-height tick per hole, so isolated single-pixel dropouts stay
        # visible even when too narrow to read as a gap in the trace.
        for i in missing:
            x = GRAPH_MARGIN_LEFT + i
            self.trace_ids.append(self.graph.create_line(
                x, GRAPH_MARGIN_TOP, x, GRAPH_MARGIN_TOP + plot_h,
                fill=COLOR_INCOMPLETE, width=1))

        # Ring each detected value outlier so it is distinguishable at a
        # glance from a genuine narrow feature in the scene.
        for i, _delta in spikes:
            x, y = to_xy(i, codes[i])
            self.trace_ids.append(self.graph.create_oval(
                x - 3, y - 3, x + 3, y + 3, outline=COLOR_SPIKE, width=1))

        present = [c for c in codes if c <= MAX_CODE]
        if present:
            lo, hi = min(present), max(present)
            range_txt = f"min {self._mv(lo):4d} mV   max {self._mv(hi):4d} mV"
        else:
            range_txt = "no data"

        flag = f"   MISSING: {len(missing)} px" if missing else ""
        self.status.config(
            text=(f" frame {frame.seq:<7d} captured {captured:4d}/{PIXELS}"
                  f"   {range_txt}{flag}")
        )

    @staticmethod
    def _gray(code: int) -> int:
        return min(255, (code * 255) // (FULL_SCALE - 1))

    @staticmethod
    def _mv(code: int) -> int:
        if code > MAX_CODE:
            return 0  # missing pixel; callers render these separately
        return (code * VREF_MV) // FULL_SCALE


def main() -> None:
    port = sys.argv[1] if len(sys.argv) > 1 else find_port()
    baud = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_BAUD

    frame_q = queue.Queue()
    log_q = queue.Queue()
    reader = SerialReader(port, baud, frame_q, log_q)
    if reader.error is not None:
        # The most common cause by far is another process already holding the
        # port; Windows grants exclusive ownership. Say so rather than just
        # surfacing the OS error.
        sys.exit(f"Could not open {port}: {reader.error}\n"
                 f"If this says 'Access is denied', another program already has "
                 f"{port} open (a serial monitor, another copy of this viewer, "
                 f"or an IDE terminal). Close it and retry.")
    reader.start()
    print(f"Reading from {port} @ {baud}")

    root = tk.Tk()
    root.title(f"S3903-1024Q live view - {port}")
    root.resizable(False, True)  # width is pinned to the sensor; log may grow
    LiveViewer(root, frame_q, log_q, port)

    def on_close():
        reader.stop()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
