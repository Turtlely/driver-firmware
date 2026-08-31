"""The live path must be untouched by the stacking work.

Frames, S lines, A lines and I lines all share one stream, and the reader that
separates them was changed. This replays a realistic live stream, including a
frame whose pixel data happens to contain both record magics, and checks that
everything still comes out where it belongs.
"""
import sys
from pathlib import Path

import numpy as np

# Paths from this file, not absolute: the checks have to run from a clone.
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "tools"))
from spectrometer.device import (Device, FRAME_MAGIC,  # noqa: E402
                                 STACK_MAGIC, parse_info)


def frame(seq, codes):
    return (FRAME_MAGIC + seq.to_bytes(4, "little")
            + len(codes).to_bytes(2, "little")
            + len(codes).to_bytes(2, "little") + codes.tobytes()
            + (int(codes.sum()) & 0xFFFF).to_bytes(2, "little"))


dev = Device()
dev.info = parse_info("I fw=1.2.0 proto=3 board=rpipico2 pixels=1024 bits=14 "
                      "vref_mv=2500 stack=1")

rng = np.random.default_rng(11)
clean = (rng.integers(0, 16384, 1024)).astype("<u2")

# A frame whose pixel data contains both magics by chance. The reader must
# not mistake either for a record boundary: the pixel count and the checksum
# are what settle it.
trap = clean.copy()
trap[100:102] = np.frombuffer(FRAME_MAGIC, dtype="<u2")
trap[300:302] = np.frombuffer(STACK_MAGIC, dtype="<u2")

stream = b"".join([
    b"BOOT: RP2350 firmware active\n",
    frame(0, clean),
    b"S 0 pulses 1024 raw 1024 miss 0 period 20us wdog 0 nostart 0 retry 0 "
    b"early 1024 fifo 1 gap 20-20us late 12us dur 20501us maxread 3us "
    b"txdrop 0 stale 0 badbits 0 px500  8123 (0x1FBB, 1239 mV)\n",
    frame(1, trap),
    b"A live=1\n",
    frame(2, clean),
    b"T frame 2 filtered 1024 expected 1024 delta +0\n",
    b"H selftest=PASS spi=PASS adcreset=PASS\n",
    b"E sd not_found\n",
])

buf = bytearray()
pos = 0
rng2 = np.random.default_rng(5)
while pos < len(stream):
    n = int(rng2.integers(1, 400))
    buf.extend(stream[pos:pos + n])
    pos += n
    while dev._consume(buf):
        pass

frames = dev.drain_frames()
stats = dev.drain_stats()
msgs = dev.drain_messages()

assert len(frames) == 3, (len(frames), [m[1] for m in msgs])
assert [f.seq for f in frames] == [0, 1, 2]
assert np.array_equal(frames[1].codes, trap), "the trap frame was corrupted"
assert dev.checksum_errors == 0
assert dev.stacks_received == 0

assert len(stats) == 1 and stats[0].seq == 0 and stats[0].pulses == 1024
kinds = [k for k, _ in msgs]
# One kind per prefix, and every line under the kind that describes it. The
# trigger count used to arrive as an "error", which is why it is named
# separately here: it is a per-frame diagnostic, not a fault.
for want in ("ack", "error", "text", "trig", "test"):
    assert want in kinds, (want, kinds)
by_kind = dict(msgs)
assert by_kind["trig"].startswith("T frame"), by_kind["trig"]
assert by_kind["error"].startswith("E sd"), by_kind["error"]
assert by_kind["test"].startswith("H selftest"), by_kind["test"]

# The last line must arrive without anything following it. This is what the
# capture completion line depends on now that the device goes silent.
tail_dev = Device()
tail_dev.info = dev.info
tb = bytearray(b"C state=done id=1 req=10 att=10 stack=10 excl=0 "
               b"exp_us=66666 dur_ms=700 result=1\n")
while tail_dev._consume(tb):
    pass
events = tail_dev.drain_capture_events()
assert len(events) == 1 and events[0].state == "done", events
assert events[0].have_result

# A tail that really could be a magic is still held for the next read.
part = bytearray(b"hello\n" + FRAME_MAGIC[:2])
while tail_dev._consume(part):
    pass
assert bytes(part) == FRAME_MAGIC[:2], bytes(part)

print("live path regression ok")
