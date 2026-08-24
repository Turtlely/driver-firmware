"""Serial transport and wire protocol for the S3903-1024Q spectrometer.

Owns the connection to the Pico and nothing else. Parsing, buffering and
reconnection live here so the user interface never touches a serial port
directly and never blocks on one.

Wire protocol 2. Frames are binary, everything else is ASCII lines
terminated by newline, on the same stream.

    <magic> <seq> <captured> <count> <count x uint16> <checksum>
                            one frame, little-endian, see below
    I <key>=<value> ...     device identification, on boot and on "id"
    S <seq> <key> <value> ...               per-frame diagnostics
    A <key>=<value>                         command acknowledgement
    E <key>=<value>                         command error
    BOOT: <text>                            free text, startup only

Frame layout, little-endian:

    uint8[4]      magic, FRAME_MAGIC
    uint32        sequence
    uint16        captured count
    uint16        pixel count
    uint16[count] pixel data
    uint16        checksum, sum of data words modulo 65536

Frames were hex-ASCII in protocol 1, which cost two characters per data
byte and made transmission the frame rate limit: the achievable rate
ceiling was about 30 fps against a 48.8 Hz sensor limit, and above that
the host fell behind far enough to lose pixels. Binary halves the cost.

Because frames are binary, the reader cannot work line by line: payload
bytes include newlines at arbitrary positions. It scans for the magic
instead, treats anything before it as text, and validates the pixel count
and checksum before accepting a frame.

Host to device commands:

    id              request an I line
    rate <hz>       set frame rate, replies with A rate_hz=<n>
    period <us>     set integration time directly, replies with
                    A frame_us=<n> rate_hz=<n>

Pixel values are 14-bit codes, 0x0000 to 0x3FFF. The value 0xFFFF is the
missing-pixel sentinel: no trigger arrived for that slot. It is outside the
14-bit range so it cannot be confused with a real reading. Missing pixels
may appear anywhere in a frame, not only at the end, because the firmware
indexes each pixel by arrival time rather than arrival order.
"""

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
PROTOCOL = 2
DEFAULT_BAUD = 115200
MISSING_PIXEL = 0xFFFF
DEFAULT_PIXELS = 1024
DEFAULT_VREF_MV = 2500
FULL_SCALE = 1 << 14
MAX_CODE = FULL_SCALE - 1


@dataclass
class DeviceInfo:
    """Parsed contents of the I line. Defaults match the current firmware
    so the application stays usable if a field is ever absent, but the
    device is the authority whenever it reports one."""

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
    raw: str = ""

    @property
    def full_scale(self):
        return 1 << self.bits

    def summary(self):
        return (f"{self.board}  fw {self.firmware}  "
                f"{self.pixels} px  {self.bits}-bit  {self.vref_mv} mV ref")


@dataclass
class Frame:
    """One acquired line. `codes` is raw ADC codes with MISSING_PIXEL in
    any slot that was not captured; `valid` marks the rest."""

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
class FrameStats:
    """Per-frame diagnostics from the S line. Field names follow the
    firmware's own vocabulary so the two can be read side by side."""

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
    reread: int = 0
    bad: int = 0
    text: str = ""


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
                      ("sysclk_mhz", "sysclk_mhz")):
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
        reread=_int(r"reread\s+(\d+)", line),
        bad=_int(r"\bbad\s+(\d+)", line),
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
        self._stop = threading.Event()
        self._thread = None
        self._hex_len = DEFAULT_PIXELS * 4
        self.frames_received = 0
        self.bytes_received = 0
        self.checksum_errors = 0
        self.last_error = None
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
        self.bytes_received = 0
        self.checksum_errors = 0
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

            if len(buf) > 8 * self._frame_bytes():
                # Runaway growth means the stream is not producing valid
                # frames. Keep the tail so a frame straddling the cut can
                # still complete, and discard the rest rather than growing
                # without bound.
                del buf[:-self._frame_bytes()]

    def _frame_bytes(self):
        return len(FRAME_MAGIC) + 4 + 2 + 2 + 2 * self.info.pixels + 2

    def _consume(self, buf):
        """Take one frame or one run of text off the front of buf.

        Returns True if the buffer advanced, False if more bytes are
        needed."""
        idx = buf.find(FRAME_MAGIC)

        if idx == -1:
            # No frame start present. Everything except a possible partial
            # magic at the tail is text.
            keep = len(FRAME_MAGIC) - 1
            if len(buf) > keep:
                self._emit_text(bytes(buf[:len(buf) - keep]))
                del buf[:len(buf) - keep]
                return True
            return False

        if idx > 0:
            self._emit_text(bytes(buf[:idx]))
            del buf[:idx]
            return True

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

    def drain_messages(self, limit=128):
        out = []
        for _ in range(limit):
            try:
                out.append(self.messages.get_nowait())
            except queue.Empty:
                break
        return out


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
