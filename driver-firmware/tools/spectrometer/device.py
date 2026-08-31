"""Serial transport and protocol parsing for the spectrometer."""

import queue
import re
import threading
import time
from dataclasses import dataclass, field

import numpy as np
import serial
from serial.tools import list_ports

PICO_VID = 0x2E8A
# Frame delimiter for protocol 2. Firmware writes this before every binary
# frame; see stageBinaryFrame in src/main.cpp for the full layout.
FRAME_MAGIC = bytes((0xA5, 0x5A, 0xD5, 0x1C))
# Delimiter for the stacked-capture result. Deliberately different from
# FRAME_MAGIC in its last two bytes, since both share the leading A5 5A and
# the reader has to tell them apart on the same stream. See the layout comment
# above buildStackHeader() in src/main.cpp.
STACK_MAGIC = bytes((0xA5, 0x5A, 0xC7, 0x2E))
STACK_HEADER_BYTES = 64
STACK_PIXEL_BYTES = 12
STACK_RECORD_VERSION = 1
# One pixel record: mean code, its standard deviation, and how many samples
# were kept and thrown away. Packed, not aligned; every field sits on its own
# natural boundary already.
STACK_PIXEL_DTYPE = np.dtype([("mean", "<f4"), ("sd", "<f4"),
                              ("accepted", "<u2"), ("rejected", "<u2")])
# Card file codecs, as src/main.cpp numbers them. "rle" is a byte-plane
# transpose followed by run-length coding: about 1.6 to 1 on a real spectrum.
# Each file records the codec it was written with, so a card may hold both.
CODEC_NONE = 0
CODEC_RLE = 1
CODEC_NAMES = {CODEC_NONE: "none", CODEC_RLE: "rle"}

PROTOCOL = 3
DEFAULT_BAUD = 115200
MISSING_PIXEL = 0xFFFF
DEFAULT_PIXELS = 1024
DEFAULT_VREF_MV = 2500
FULL_SCALE = 1 << 14
MAX_CODE = FULL_SCALE - 1


@dataclass
class DeviceInfo:
    """Parsed contents of the firmware identification line."""

    firmware: str = "unknown"
    board: str = "unknown"
    pixels: int = DEFAULT_PIXELS
    bits: int = 14
    vref_mv: int = DEFAULT_VREF_MV
    rate_hz: int = 0
    rate_min: int = 1
    rate_max: int = 40
    pixel_period_us: int = 0
    frame_us: int = 0
    frame_us_min: int = 20600
    frame_us_max: int = 10000000
    refractory_us: int = 0
    sysclk_mhz: int = 0
    pio_crc: str = ""
    built: str = ""
    proto: int = 1
    stack: int = 0
    stack_block: int = 16
    stack_block_max: int = 32
    stack_sigma_x100: int = 300
    cap_max_frames: int = 0
    cap_max_s: int = 0
    mode: int = 0
    sd: int = 0
    cal: int = 0
    # Points the device currently holds, and whether it has anywhere to put
    # them. cal_store is 0 when the flash filesystem would not mount: the
    # commands still work, but nothing survives a power cycle.
    cal_n: int = 0
    cal_store: int = 0
    # Outcome of the self test the device runs at boot: 1 pass, 0 fail, -1
    # when the field is absent. Absent means older firmware, which ran no
    # test, and that is unknown rather than failed.
    selftest: int = -1
    raw: str = ""

    @property
    def supports_stacking(self):
        return bool(self.stack)

    @property
    def supports_calibration(self):
        """Whether the firmware has the "cal" commands."""
        return bool(self.cal)

    @property
    def self_test_passed(self):
        """True, False, or None when the device did not say.

        The device checks the SPI link and resets the ADC at boot. The lines
        it prints scroll past long before anyone connects, so this field is
        where the outcome is actually read from. Re-run it with "selftest".
        """
        if self.selftest < 0:
            return None
        return self.selftest == 1

    @property
    def supports_storage(self):
        """Whether the firmware has the SD card commands.

        The identification line advertises the capability only. Whether a
        card is actually present is a separate question, answered by
        "sd status", because a card can be removed while the instrument
        runs."""
        return bool(self.sd)

    @property
    def full_scale(self):
        return 1 << self.bits

    def summary(self):
        return (f"{self.board}  fw {self.firmware}  "
                f"{self.pixels} px  {self.bits}-bit  {self.vref_mv} mV ref")


@dataclass
class Frame:
    """One acquired line with a validity mask derived from its codes."""

    seq: int
    codes: np.ndarray
    captured: int
    t_host: float = field(default_factory=time.time)

    @property
    def valid(self):
        return self.codes <= MAX_CODE

    @property
    def missing_count(self):
        return int(np.count_nonzero(~self.valid))


@dataclass
class StackResult:
    """One completed on-device stack.

    `mean` and `sd` are in ADC codes, not volts, and are NaN where no sample
    survived. `sd` is the RAW frame-to-frame spread of the accepted samples:
    on this instrument most of it is the 17.4 kHz interferer rather than
    random noise, so treat it as an upper bound on the noise and not as the
    noise floor. See ISSUES.md entry 31.

    A frame in which a pixel had no trigger contributes to neither
    `accepted` nor `rejected`, so the missing count for a pixel is
    frames_stacked - accepted - rejected.
    """

    capture_id: int = 0
    dark: bool = False
    frames_requested: int = 0
    frames_attempted: int = 0
    frames_stacked: int = 0
    frames_excluded: int = 0
    exposure_us: int = 0
    duration_ms: int = 0
    block_frames: int = 0
    sigma: float = 0.0
    clip_iters: int = 0
    stat_missing: int = 0
    stat_stale: int = 0
    stat_badbits: int = 0
    stat_max_late_us: int = 0
    stat_nostart: int = 0
    mean: np.ndarray = None
    sd: np.ndarray = None
    accepted: np.ndarray = None
    rejected: np.ndarray = None
    t_host: float = field(default_factory=time.time)

    @property
    def pixels(self):
        return 0 if self.mean is None else int(self.mean.size)

    @property
    def total_rejected(self):
        return 0 if self.rejected is None else int(self.rejected.sum())

    def summary(self):
        return (f"{self.frames_stacked} frames stacked of "
                f"{self.frames_requested} requested, "
                f"{self.frames_excluded} excluded, "
                f"{self.exposure_us / 1000.0:.2f} ms each, "
                f"{self.duration_ms / 1000.0:.1f} s total, "
                f"{self.total_rejected} samples clipped at "
                f"{self.sigma:.2f} sigma")


@dataclass
class CaptureStatus:
    """Parsed contents of a C line: what a capture is doing right now."""

    state: str = "idle"
    capture_id: int = 0
    requested: int = 0
    attempted: int = 0
    stacked: int = 0
    excluded: int = 0
    exposure_us: int = 0
    elapsed_ms: int = 0
    duration_ms: int = 0
    have_result: bool = False
    mode: int = 0
    text: str = ""

    @property
    def running(self):
        return self.state == "run"

    @property
    def finished(self):
        return self.state in ("done", "stopped")

    @property
    def progress(self):
        if self.requested <= 0:
            return 0.0
        return min(1.0, self.attempted / self.requested)


@dataclass
class FrameStats:
    """Per-frame diagnostics using the firmware field names."""

    seq: int = 0
    pulses: int = 0
    raw_edges: int = 0
    miss: int = 0
    period_us: int = 0
    wdog: int = 0
    nostart: int = 0
    retry: int = 0
    early: int = 0
    fifo: int = 0
    gap_min: int = 0
    gap_max: int = 0
    late_us: int = 0
    dur_us: int = 0
    maxread_us: int = 0
    txdrop: int = 0
    stale: int = 0
    text: str = ""


@dataclass
class SdFile:
    """One stored capture, as the device lists it.

    Every field except `name`, `bytes` and `date` is read by the firmware
    out of the file's own header, so this describes the file on the card
    rather than anything the device remembers about the session that wrote
    it. `valid` is false when the header could not be read, which is how a
    truncated or foreign file appears: still listed, so it can be deleted,
    but marked as unreadable."""

    name: str = ""
    bytes: int = 0
    # What the file would occupy uncompressed, and how it is stored. The
    # record itself is identical either way: compression is a property of
    # the file, so a stored spectrum and a fresh one decode the same.
    plain_bytes: int = 0
    codec: int = 0
    tag: str = "SP"
    capture_id: int = 0
    frames_requested: int = 0
    frames_stacked: int = 0
    exposure_us: int = 0
    duration_ms: int = 0
    dark: bool = False
    valid: bool = True
    date: str = ""
    text: str = ""

    @property
    def kind(self):
        return {"SP": "spectrum", "DK": "dark", "FL": "flat"}.get(
            self.tag, self.tag)

    @property
    def codec_name(self):
        return CODEC_NAMES.get(self.codec, f"codec {self.codec}")

    @property
    def ratio(self):
        """How much the codec saved, or 1.0 when it is not known."""
        if self.bytes > 0 and self.plain_bytes > 0:
            return self.plain_bytes / self.bytes
        return 1.0


@dataclass
class SdStatus:
    """Reply to "sd status"."""

    state: str = "absent"
    mounted: bool = False
    clock_mhz: int = 0
    fat_type: int = 0
    # The physical card, from its capacity register.
    size_mb: int = 0
    # The mounted partition. Different from size_mb whenever the card carries
    # a partition smaller than itself, and it is the volume that fills up, so
    # every "how full" figure is computed from this one. 0 from firmware that
    # did not report it.
    vol_mb: int = 0
    # -1 when the free space was not asked for. Computing it walks the whole
    # file allocation table, which takes seconds on a large card, so the
    # device only does it on request.
    free_mb: int = -1
    next_seq: int = 0
    files: int = 0
    writes: int = 0
    write_errors: int = 0
    auto: bool = False
    clock_set: bool = False
    codec: int = 0
    codec_name: str = "none"
    last_file: str = ""
    last_plain: int = 0
    last_stored: int = 0
    error: str = ""
    text: str = ""

    @property
    def last_ratio(self):
        if self.last_stored > 0 and self.last_plain > 0:
            return self.last_plain / self.last_stored
        return 1.0

    @property
    def capacity_mb(self):
        """The number every "how full" figure is measured against.

        The mounted volume, falling back to the card for firmware that did
        not report the volume separately. Files live on the volume, so the
        volume is what fills up; a card carrying a smaller partition reports
        two different numbers and only one of them is the answer.
        """
        return self.vol_mb if self.vol_mb > 0 else self.size_mb

    @property
    def size_gb(self):
        """The mounted volume, in GB."""
        return self.capacity_mb / 1024.0

    @property
    def card_gb(self):
        """The physical card, in GB. Equal to size_gb on a card whose
        partition spans it."""
        return self.size_mb / 1024.0

    @property
    def partition_is_short(self):
        """Whether the mounted volume is materially smaller than the card.

        A tenth of the card is the threshold: rounding and the reserved
        areas account for a little, a missing partition accounts for a lot.
        """
        if self.vol_mb <= 0 or self.size_mb <= 0:
            return False
        return self.vol_mb < self.size_mb * 0.9

    @property
    def free_gb(self):
        """Free space in GB, or None when it was not asked for."""
        return None if self.free_mb < 0 else self.free_mb / 1024.0

    @property
    def used_gb(self):
        """Used space in GB, or None when the free space is unknown."""
        if self.free_mb < 0:
            return None
        return max(0.0, (self.capacity_mb - self.free_mb) / 1024.0)

    @property
    def used_fraction(self):
        """Used space as 0.0 to 1.0, or None when the free space is
        unknown."""
        if self.free_mb < 0 or self.capacity_mb <= 0:
            return None
        return min(1.0, max(0.0,
                            (self.capacity_mb - self.free_mb)
                            / self.capacity_mb))

    def summary(self):
        """One line: how the card is formatted and how full it is.

        The counters the device also reports are diagnostics and belong in
        the console, not above a list of files.
        """
        if not self.mounted:
            return f"no card ({self.error})"
        name = "exFAT" if self.fat_type == 64 else f"FAT{self.fat_type}"
        # Named when it happens, because the difference is a formatting
        # fault: most of the card is unreachable and nothing else would say
        # so.
        short = (f" (card {self.card_gb:.1f} GB)"
                 if self.partition_is_short else "")
        used = self.used_gb
        if used is None:
            return f"{name}, {self.size_gb:.1f} GB{short}"
        return (f"{name}, {used:.2f} of {self.size_gb:.1f} GB used "
                f"({self.used_fraction * 100.0:.1f} %){short}")


@dataclass
class CalInfo:
    """The calibration the device holds, as it reports it.

    `points` is the raw "<pixel>:<nm>,..." text. Nothing here interprets it:
    Calibration.from_device_text() does that, so the parsing and the fitting
    stay in one module.
    """

    points: str = ""
    count: int = 0
    bytes: int = 0
    # False when the flash filesystem would not mount. The commands still
    # work; nothing survives a power cycle.
    store: bool = False
    text: str = ""

    @property
    def present(self):
        return self.count >= 2 and bool(self.points)

    def summary(self):
        if not self.store:
            return "no calibration store on the device"
        if not self.present:
            return "device holds no calibration"
        return f"device holds {self.count} points ({self.bytes} bytes)"


def _int(pattern, text, default=0):
    m = re.search(pattern, text)
    return int(m.group(1)) if m else default


def parse_info(line):
    info = DeviceInfo(raw=line)
    fields = dict(re.findall(r"(\w+)=(\S+)", line))
    info.firmware = fields.get("fw", info.firmware)
    info.board = fields.get("board", info.board)
    info.pio_crc = fields.get("pio_crc", "")
    try:
        info.proto = int(fields.get("proto", "1"))
    except ValueError:
        info.proto = 1
    info.built = fields.get("built", "")
    for name, key in (("pixels", "pixels"), ("bits", "bits"),
                      ("vref_mv", "vref_mv"), ("rate_hz", "rate_hz"),
                      ("rate_min", "rate_min"), ("rate_max", "rate_max"),
                      ("pixel_period_us", "pixel_period_us"),
                      ("frame_us", "frame_us"),
                      ("frame_us_min", "frame_us_min"),
                      ("frame_us_max", "frame_us_max"),
                      ("refractory_us", "refractory_us"),
                      ("sysclk_mhz", "sysclk_mhz"), ("stack", "stack"),
                      ("stack_block", "stack_block"),
                      ("stack_block_max", "stack_block_max"),
                      ("stack_sigma_x100", "stack_sigma_x100"),
                      ("cap_max_frames", "cap_max_frames"),
                      ("cap_max_s", "cap_max_s"), ("mode", "mode"),
                      ("sd", "sd"), ("cal", "cal"), ("cal_n", "cal_n"),
                      ("cal_store", "cal_store"), ("selftest", "selftest")):
        if key in fields:
            try:
                setattr(info, name, int(fields[key]))
            except ValueError:
                pass
    return info


def parse_stats(line):
    gap = re.search(r"gap\s+(\d+)\s*-\s*(\d+)\s*us", line)
    return FrameStats(
        seq=_int(r"^S\s+(\d+)", line),
        pulses=_int(r"pulses\s+(\d+)", line),
        raw_edges=_int(r"\braw\s+(\d+)", line),
        miss=_int(r"\bmiss\s+(\d+)", line),
        period_us=_int(r"period\s+(\d+)us", line),
        wdog=_int(r"wdog\s+(\d+)", line),
        nostart=_int(r"nostart\s+(\d+)", line),
        retry=_int(r"retry\s+(\d+)", line),
        early=_int(r"early\s+(\d+)", line),
        fifo=_int(r"fifo\s+(\d+)", line),
        gap_min=int(gap.group(1)) if gap else 0,
        gap_max=int(gap.group(2)) if gap else 0,
        late_us=_int(r"late\s+(\d+)us", line),
        dur_us=_int(r"dur\s+(\d+)us", line),
        maxread_us=_int(r"maxread\s+(\d+)us", line),
        txdrop=_int(r"txdrop\s+(\d+)", line),
        stale=_int(r"stale\s+(\d+)", line),
        text=line,
    )


def parse_capture_status(line):
    m = re.search(r"state=(\w+)", line)
    return CaptureStatus(
        state=m.group(1) if m else "idle",
        capture_id=_int(r"\bid=(\d+)", line),
        requested=_int(r"\breq=(\d+)", line),
        attempted=_int(r"\batt=(\d+)", line),
        stacked=_int(r"\bstack=(\d+)", line),
        excluded=_int(r"\bexcl=(\d+)", line),
        exposure_us=_int(r"\bexp_us=(\d+)", line),
        elapsed_ms=_int(r"\belapsed_ms=(\d+)", line),
        duration_ms=_int(r"\bdur_ms=(\d+)", line),
        have_result=bool(_int(r"\bresult=(\d+)", line)),
        mode=_int(r"\bmode=(\d+)", line),
        text=line,
    )


def _sd_str(key, text, default=""):
    m = re.search(r"\b" + key + r"=(\S+)", text)
    return m.group(1) if m else default


def parse_cal(line):
    """Reply to "cal get". See handleCalCommand() in src/main.cpp."""
    points = _sd_str("points", line, "none")
    return CalInfo(
        points="" if points == "none" else points,
        count=_int(r"\bn=(\d+)", line),
        bytes=_int(r"\bbytes=(\d+)", line),
        store=bool(_int(r"\bstore=(\d+)", line)),
        text=line,
    )


def parse_sd_file(line):
    return SdFile(
        name=_sd_str("name", line),
        bytes=_int(r"\bbytes=(\d+)", line),
        plain_bytes=_int(r"\bplain=(\d+)", line),
        codec=_int(r"\bcodec=(\d+)", line),
        tag=_sd_str("tag", line, "SP"),
        capture_id=_int(r"\bid=(\d+)", line),
        frames_requested=_int(r"\breq=(\d+)", line),
        frames_stacked=_int(r"\bstack=(\d+)", line),
        exposure_us=_int(r"\bexp_us=(\d+)", line),
        duration_ms=_int(r"\bdur_ms=(\d+)", line),
        dark=bool(_int(r"\bdark=(\d+)", line)),
        valid=bool(_int(r"\bvalid=(\d+)", line, 1)),
        date=_sd_str("date", line),
        text=line,
    )


def parse_sd_status(line):
    return SdStatus(
        state=_sd_str("state", line, "absent"),
        mounted=bool(_int(r"\bmounted=(\d+)", line)),
        clock_mhz=_int(r"\bmhz=(\d+)", line),
        fat_type=_int(r"\bfat=(\d+)", line),
        size_mb=_int(r"\bsize_mb=(\d+)", line),
        vol_mb=_int(r"\bvol_mb=(\d+)", line),
        free_mb=_int(r"\bfree_mb=(-?\d+)", line, -1),
        next_seq=_int(r"\bnext=(\d+)", line),
        files=_int(r"\bfiles=(\d+)", line),
        writes=_int(r"\bwrites=(\d+)", line),
        write_errors=_int(r"\bwerr=(\d+)", line),
        auto=bool(_int(r"\bauto=(\d+)", line)),
        clock_set=bool(_int(r"\bclock=(\d+)", line)),
        codec=_int(r"\bcodec_n=(\d+)", line),
        codec_name=_sd_str("codec", line, "none"),
        last_file=_sd_str("last", line),
        last_plain=_int(r"\blast_plain=(\d+)", line),
        last_stored=_int(r"\blast_stored=(\d+)", line),
        error=_sd_str("error", line, "none"),
        text=line,
    )


def find_devices():
    """Ports that look like a Pico, most likely first. Returns a list of
    (device, description) with Pico VID matches ahead of everything else so
    autodetect picks the right one without excluding manual selection."""
    pico, other = [], []
    for p in list_ports.comports():
        entry = (p.device, p.description or "")
        (pico if p.vid == PICO_VID else other).append(entry)
    return pico + other


class DeviceError(Exception):
    pass


class Device:
    """Threaded reader. Frames and diagnostics are pushed onto queues; the
    caller drains them whenever convenient. Nothing here blocks the caller
    and nothing here touches the user interface.

    The reader thread owns the port exclusively for its lifetime. Windows
    grants a COM port to a single process, so a second application holding
    the port is the usual cause of a failed connect, and connect() says so
    explicitly rather than surfacing only the operating system error.
    """

    def __init__(self):
        self.ser = None
        self.info = DeviceInfo()
        self.port = None
        self.frames = queue.Queue(maxsize=256)
        self.stats = queue.Queue(maxsize=512)
        self.messages = queue.Queue(maxsize=512)
        # Stack results are large and rare, and losing one throws away a
        # whole capture, so this queue is short and the caller is expected
        # to drain it every poll rather than let it wrap.
        self.stacks = queue.Queue(maxsize=4)
        self.capture_events = queue.Queue(maxsize=64)
        # Card replies: listings, status and the notice that a capture was
        # written. A directory page is at most SD_LIST_MAX lines plus its
        # terminator, so this holds several pages without wrapping.
        self.sd_events = queue.Queue(maxsize=128)
        # Calibration replies. One line per request, so this only ever holds
        # more than one entry if the caller stops draining.
        self.cal_events = queue.Queue(maxsize=16)
        self._stop = threading.Event()
        self._thread = None
        self._hex_len = DEFAULT_PIXELS * 4
        self.frames_received = 0
        self.stacks_received = 0
        self.bytes_received = 0
        self.checksum_errors = 0
        self.stack_checksum_errors = 0
        self.last_capture = None
        self.last_error = None
        # Last card status reply. Named apart from DeviceInfo.sd, which is
        # the capability flag from the identification line, not the card.
        self.sd_last = SdStatus()
        # Last calibration reply. Named apart from DeviceInfo.cal, which is
        # the capability flag from the identification line.
        self.cal_last = CalInfo()
        self._text_buf = bytearray()

    @property
    def connected(self):
        return self.ser is not None and self._thread is not None and self._thread.is_alive()

    def connect(self, port, baud=DEFAULT_BAUD, identify_timeout=2.5):
        if self.connected:
            self.disconnect()
        try:
            self.ser = serial.Serial(port, baud, timeout=0.2)
        except serial.SerialException as exc:
            self.ser = None
            msg = str(exc)
            if "Access is denied" in msg or "PermissionError" in msg:
                raise DeviceError(
                    f"{port} is already open in another program. Close any "
                    f"serial monitor or second copy of this application, then "
                    f"retry.") from exc
            raise DeviceError(f"Could not open {port}: {msg}") from exc

        self.port = port
        self._stop.clear()
        self.frames_received = 0
        self.stacks_received = 0
        self.bytes_received = 0
        self.checksum_errors = 0
        self.stack_checksum_errors = 0
        self.last_capture = None
        self.last_error = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

        # Ask for identification rather than waiting for the boot banner:
        # the device may have been running long before this connection.
        self.request_info()
        deadline = time.time() + identify_timeout
        while time.time() < deadline:
            if self.info.firmware != "unknown":
                break
            time.sleep(0.05)
        self._hex_len = self.info.pixels * 4
        return self.info

    def disconnect(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None
        self.port = None

    def send(self, command):
        if not self.connected:
            raise DeviceError("Not connected")
        try:
            self.ser.write((command.strip() + "\n").encode("ascii"))
        except (serial.SerialException, OSError) as exc:
            raise DeviceError(f"Write failed: {exc}") from exc

    def request_info(self):
        try:
            self.send("id")
        except DeviceError:
            pass

    def set_rate(self, hz):
        self.send(f"rate {int(hz)}")

    def set_integration_us(self, us):
        """Set integration time directly, in microseconds.

        Preferred over set_rate: integration time is the quantity that
        matters physically, and setting it avoids the resolution loss of
        rounding through integer hertz."""
        self.send(f"period {int(round(us))}")

    def set_integration_ms(self, ms):
        self.set_integration_us(float(ms) * 1000.0)

    def set_live(self, on):
        """Start or stop frame transmission.

        The device keeps scanning either way. The detector integrates whether
        or not anything reads it, so a device that stopped pulsing START would
        return a saturated frame when it started again."""
        self.send("live 1" if on else "live 0")

    def capture_start(self, secs, exposure_us, sigma=3.0, block=16,
                      dark=False, tag=None):
        """Begin an on-device stacked capture.

        The device converts seconds to a frame count using the exposure it is
        given here, and reports both in its reply. Doing the conversion there
        rather than here is what keeps the two from disagreeing.

        `tag` is the two letter prefix of the file the device writes to its
        card: SP spectrum, DK dark, FL flat. The device defaults it from the
        dark flag, so a caller that does not care may leave it out."""
        cmd = (f"cap start secs={float(secs):.3f} "
               f"exp={int(round(exposure_us))} "
               f"sigma={float(sigma):.2f} block={int(block)} "
               f"dark={1 if dark else 0}")
        if tag:
            cmd += f" tag={str(tag)[:2].upper()}"
        self.send(cmd)

    def capture_stop(self):
        self.send("cap stop")

    def request_capture_status(self):
        self.send("cap status")

    def request_capture_result(self):
        """Ask for the stored result. Repeatable: the device holds it until
        the next capture overwrites it, so a transfer interrupted by a link
        drop costs a retry rather than the capture."""
        self.send("cap get")

    # ------------------------------------------------------------ storage

    def sd_status(self, free=False):
        """Ask about the card.

        `free` also asks for the free space. It is off by default because
        the device has to walk the whole file allocation table to answer,
        which takes seconds on a large card."""
        self.send("sd status free=1" if free else "sd status")

    def sd_mount(self):
        """Attach a card inserted after the device booted."""
        self.send("sd mount")

    def sd_set_codec(self, name):
        """Choose how NEW files are written. Existing files are untouched.

        Each file records its own codec, so changing this never makes an
        older file unreadable."""
        self.send(f"sd codec {name}")

    def sd_set_auto(self, on):
        """Write every finished capture to the card, or stop doing so."""
        self.send(f"sd auto {1 if on else 0}")

    def sd_set_time(self, epoch=None):
        """Hand the device the wall clock, for file dates.

        LOCAL seconds, not UTC. FAT stores wall-clock time with no zone, so
        a card read on this computer afterwards shows the time the capture
        was actually taken. The device has no clock of its own and dates
        everything 2000-01-01 until this is sent."""
        if epoch is None:
            # time.time() is UTC; shift it by this zone's offset so the
            # device stores local wall-clock time.
            now = time.time()
            epoch = now - time.timezone
            if time.localtime(now).tm_isdst and time.daylight:
                epoch = now - time.altzone
        self.send(f"sd time {int(epoch)}")

    def sd_list(self, start=0, count=24):
        """Ask for one page of the directory.

        Paged because the device transmit queue holds about 3 KB and a
        listing line is roughly 130 bytes. The reply is one `("file", SdFile)`
        event per entry followed by one `("list_end", dict)`."""
        self.send(f"sd list start={int(start)} count={int(count)}")

    def sd_get(self, name):
        """Ask for one stored file.

        It arrives as a StackResult on the stack queue, exactly as a fresh
        capture does: the file holds the record byte for byte, so there is
        one decoder and not two."""
        self.send(f"sd get name={name}")

    def sd_save(self, tag="SP"):
        """Write the result currently held in device RAM to the card."""
        self.send(f"sd save tag={str(tag)[:2].upper()}")

    def sd_delete(self, name):
        self.send(f"sd del name={name}")

    def sd_rename(self, name, new_name):
        """Give one stored file a different name.

        The device refuses a name that is already taken rather than
        overwriting it, and refuses anything that is not a plain 8.3 name."""
        self.send(f"sd rename name={name} to={new_name}")

    # ------------------------------------------------------- calibration

    def cal_get(self):
        """Ask what calibration the device holds.

        The reply is one `CalInfo` on the calibration queue."""
        self.send("cal get")

    def cal_set(self, points_text):
        """Replace the device calibration and write it to its flash.

        `points_text` is "<pixel>:<nm>,..." from
        Calibration.to_device_text(). The device stores the points, not the
        polynomial: the fit belongs to calibration.py and must have one
        home."""
        self.send(f"cal set {points_text}")

    def cal_clear(self):
        self.send("cal clear")

    def _run(self):
        """Byte-oriented reader.

        Frames are binary and status lines are ASCII on the same stream, so
        this cannot use readline(): a frame payload contains newlines at
        arbitrary positions. Instead the buffer is scanned for the frame
        magic, bytes before it are treated as text, and the frame is
        validated before being accepted.
        """
        buf = bytearray()
        self._text_buf = bytearray()
        while not self._stop.is_set():
            try:
                waiting = self.ser.in_waiting
                chunk = self.ser.read(waiting if waiting else 1)
            except (serial.SerialException, TypeError, OSError, ValueError):
                # The non-SerialException cases occur only on shutdown, when
                # disconnect() closes the handle while a read is in flight.
                if not self._stop.is_set():
                    self.last_error = "Serial connection lost"
                break
            if not chunk:
                continue
            self.bytes_received += len(chunk)
            buf.extend(chunk)

            # Loop until no further progress is possible, so a single read
            # containing several frames is fully consumed.
            while self._consume(buf):
                pass

            limit = max(8 * self._frame_bytes(), 3 * self._stack_bytes())
            if len(buf) > limit:
                # Runaway growth means the stream is not producing valid
                # records. Keep enough tail that a record straddling the cut
                # can still complete, and discard the rest rather than
                # growing without bound. Sized against the stack record now,
                # which is six times a frame: the old limit was shorter than
                # one result and would have cut every transfer in half.
                keep = self._stack_bytes()
                del buf[:-keep]

    def _frame_bytes(self):
        return len(FRAME_MAGIC) + 4 + 2 + 2 + 2 * self.info.pixels + 2

    def _stack_bytes(self, pixels=None):
        if pixels is None:
            pixels = self.info.pixels
        return STACK_HEADER_BYTES + STACK_PIXEL_BYTES * pixels + 4

    def _consume(self, buf):
        """Take one record or one run of text off the front of buf.

        Returns True if the buffer advanced, False if more bytes are
        needed."""
        fi = buf.find(FRAME_MAGIC)
        si = buf.find(STACK_MAGIC)
        found = [i for i in (fi, si) if i != -1]

        if not found:
            # No record start present, so everything here is text, except a
            # tail that could be the beginning of a magic split across two
            # reads.
            #
            # Withhold that tail ONLY when it really is a magic prefix. An
            # unconditional three bytes was held back before, and that was a
            # bug the device made visible as soon as it learned to go quiet:
            # the last line before a silence ends in its own newline, those
            # final bytes were held as a maybe-magic, and the line was never
            # dispatched. The capture completion line is exactly that line,
            # and it arrived only when the next status poll pushed it out.
            keep = _magic_prefix_len(buf)
            if len(buf) > keep:
                self._emit_text(bytes(buf[:len(buf) - keep]))
                del buf[:len(buf) - keep]
                return True
            return False

        idx = min(found)
        if idx > 0:
            self._emit_text(bytes(buf[:idx]))
            del buf[:idx]
            return True

        if idx == si:
            return self._consume_stack(buf)

        need = self._frame_bytes()
        if len(buf) < need:
            return False

        seq = int.from_bytes(buf[4:8], "little")
        captured = int.from_bytes(buf[8:10], "little")
        count = int.from_bytes(buf[10:12], "little")

        if count != self.info.pixels:
            # The magic matched inside data. Resynchronise by one byte
            # rather than discarding the whole buffer.
            del buf[0:1]
            return True

        payload = bytes(buf[12:12 + 2 * count])
        want = int.from_bytes(buf[12 + 2 * count:14 + 2 * count], "little")
        codes = np.frombuffer(payload, dtype="<u2")
        if (int(codes.sum()) & 0xFFFF) != want:
            self.checksum_errors += 1
            _put(self.messages, ("error", f"frame {seq} checksum mismatch"))
            del buf[0:1]
            return True

        del buf[:need]
        self.frames_received += 1
        _put(self.frames, Frame(seq=seq, codes=codes.copy(),
                                captured=min(captured, count)))
        return True

    def _consume_stack(self, buf):
        """Take one stacked-capture result off the front of buf.

        Same discipline as a frame: the pixel count and the checksum are both
        validated before anything is accepted, and a failure resynchronises by
        one byte rather than discarding the buffer, because the magic can
        occur by chance inside pixel data."""
        if len(buf) < STACK_HEADER_BYTES:
            return False

        version = int.from_bytes(buf[4:6], "little")
        count = int.from_bytes(buf[36:38], "little")
        if version != STACK_RECORD_VERSION or count != self.info.pixels:
            del buf[0:1]
            return True

        need = self._stack_bytes(count)
        if len(buf) < need:
            return False

        want = int.from_bytes(buf[need - 4:need], "little")
        if (sum(buf[:need - 4]) & 0xFFFFFFFF) != want:
            self.stack_checksum_errors += 1
            _put(self.messages, ("error", "stack result checksum mismatch"))
            del buf[0:1]
            return True

        def u16(off):
            return int.from_bytes(buf[off:off + 2], "little")

        def u32(off):
            return int.from_bytes(buf[off:off + 4], "little")

        body = np.frombuffer(
            bytes(buf[STACK_HEADER_BYTES:need - 4]), dtype=STACK_PIXEL_DTYPE)

        result = StackResult(
            capture_id=u32(8),
            dark=bool(u16(6) & 1),
            frames_requested=u32(12),
            frames_attempted=u32(16),
            frames_stacked=u32(20),
            frames_excluded=u32(24),
            exposure_us=u32(28),
            duration_ms=u32(32),
            block_frames=u16(38),
            sigma=u16(40) / 100.0,
            clip_iters=u16(42),
            stat_missing=u32(44),
            stat_stale=u32(48),
            stat_badbits=u32(52),
            stat_max_late_us=u32(56),
            stat_nostart=u32(60),
            mean=body["mean"].astype(np.float64),
            sd=body["sd"].astype(np.float64),
            accepted=body["accepted"].astype(np.int32),
            rejected=body["rejected"].astype(np.int32),
        )

        del buf[:need]
        self.stacks_received += 1
        _put(self.stacks, result)
        return True

    def _emit_text(self, raw):
        """Accumulate text and dispatch only complete lines.

        Text arrives in whatever chunks the serial layer produces, and a
        line is frequently split across two of them, so dispatching each
        chunk as it appears fragments lines into pieces. Observed directly
        as single characters arriving as separate messages and a command
        reply of "A rate_hz=22" being delivered as "A rate_hz=" followed
        by "22". The remainder is carried until its newline arrives.
        """
        self._text_buf.extend(raw)
        while True:
            nl = self._text_buf.find(b"\n")
            if nl == -1:
                break
            line = bytes(self._text_buf[:nl])
            del self._text_buf[:nl + 1]
            try:
                text = line.decode("ascii", "replace").strip()
            except Exception:
                continue
            if text:
                self._dispatch(text)
        # A line longer than any the firmware emits means the stream is
        # not text at all; discard rather than accumulate without bound.
        if len(self._text_buf) > 1024:
            del self._text_buf[:]

    def _dispatch(self, line):
        kind = line[:2]
        if kind == "S ":
            _put(self.stats, parse_stats(line))
        elif kind == "I ":
            self.info = parse_info(line)
            self._hex_len = self.info.pixels * 4
            _put(self.messages, ("info", line))
        elif kind == "C ":
            status = parse_capture_status(line)
            self.last_capture = status
            _put(self.capture_events, status)
            _put(self.messages, ("cap", line))
        elif kind == "D ":
            # Card replies. Every one of them is also logged, because a
            # listing that disagrees with the console is a fault worth
            # seeing.
            body = line[2:]
            if body.startswith("file "):
                _put(self.sd_events, ("file", parse_sd_file(line)))
            elif body.startswith("list_end"):
                _put(self.sd_events, ("list_end", {
                    "start": _int(r"\bstart=(\d+)", line),
                    "shown": _int(r"\bshown=(\d+)", line),
                    "total": _int(r"\btotal=(\d+)", line),
                }))
            elif body.startswith("state="):
                status = parse_sd_status(line)
                self.sd_last = status
                _put(self.sd_events, ("status", status))
            elif body.startswith("saved"):
                _put(self.sd_events, ("saved", _sd_str("name", line)))
                _put(self.messages, ("cap", line))
            elif body.startswith("deleted"):
                _put(self.sd_events, ("deleted", _sd_str("name", line)))
            elif body.startswith("renamed"):
                _put(self.sd_events, ("renamed", {
                    "name": _sd_str("name", line),
                    "to": _sd_str("to", line),
                }))
            else:
                _put(self.messages, ("text", line))
        elif kind == "K ":
            # Calibration. Its own letter, not "A ", because the reply
            # carries the whole point list and the console would otherwise
            # be the only thing that ever read it.
            info = parse_cal(line)
            self.cal_last = info
            self.info.cal_n = info.count
            self.info.cal_store = int(info.store)
            _put(self.cal_events, info)
            _put(self.messages, ("cal", line))
        elif kind == "T ":
            # Per-frame trigger counts. One line per frame, so this belongs
            # with the frame statistics and not with the replies: it used to
            # arrive on the "E " prefix and was shown as an error, in red, at
            # the frame rate.
            _put(self.messages, ("trig", line))
        elif kind == "H ":
            # Hardware test results. Their own prefix because they carry
            # PASS and FAIL, and on the "A " prefix a line ending FAIL was
            # rendered in the acknowledgement colour, which is green.
            _put(self.messages, ("test", line))
        elif kind == "A ":
            _put(self.messages, ("ack", line))
        elif kind == "E ":
            _put(self.messages, ("error", line))
        else:
            _put(self.messages, ("text", line))

    def drain_frames(self, limit=64):
        out = []
        for _ in range(limit):
            try:
                out.append(self.frames.get_nowait())
            except queue.Empty:
                break
        return out

    def drain_stats(self, limit=128):
        out = []
        for _ in range(limit):
            try:
                out.append(self.stats.get_nowait())
            except queue.Empty:
                break
        return out

    def drain_stacks(self, limit=4):
        out = []
        for _ in range(limit):
            try:
                out.append(self.stacks.get_nowait())
            except queue.Empty:
                break
        return out

    def drain_sd_events(self, limit=128):
        out = []
        for _ in range(limit):
            try:
                out.append(self.sd_events.get_nowait())
            except queue.Empty:
                break
        return out

    def drain_cal_events(self, limit=16):
        out = []
        for _ in range(limit):
            try:
                out.append(self.cal_events.get_nowait())
            except queue.Empty:
                break
        return out

    def drain_capture_events(self, limit=64):
        out = []
        for _ in range(limit):
            try:
                out.append(self.capture_events.get_nowait())
            except queue.Empty:
                break
        return out

    def drain_messages(self, limit=128):
        out = []
        for _ in range(limit):
            try:
                out.append(self.messages.get_nowait())
            except queue.Empty:
                break
        return out


def _magic_prefix_len(buf):
    """Length of the tail of buf that could still become a record magic.

    Returns 0 when the tail cannot begin one, so text is not held back
    waiting for bytes that are not coming."""
    longest = max(len(FRAME_MAGIC), len(STACK_MAGIC)) - 1
    for k in range(min(longest, len(buf)), 0, -1):
        tail = bytes(buf[-k:])
        if FRAME_MAGIC.startswith(tail) or STACK_MAGIC.startswith(tail):
            return k
    return 0


def _put(q, item):
    """Drop the oldest entry rather than block when a queue is full. The
    reader thread must never stall on a consumer that has stopped draining,
    or the serial buffer overflows and frames are lost at the port."""
    try:
        q.put_nowait(item)
    except queue.Full:
        try:
            q.get_nowait()
            q.put_nowait(item)
        except queue.Empty:
            pass
