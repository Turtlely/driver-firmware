"""Feeds a real device record through the host reader and checks it back.

The record comes from record_test.exe, which is the firmware's own
buildStackHeader and buildStackPixels compiled natively. So this tests the
wire format against the code that writes it, not against a second description
of it.

The record is delivered in awkward chunk sizes, with text and a frame mixed
in, because that is what the serial layer actually hands the reader.
"""
import subprocess
import sys
from pathlib import Path

import numpy as np

# Paths from this file, not absolute: the checks have to run from a clone.
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "tools"))
from spectrometer.device import (Device, FRAME_MAGIC,  # noqa: E402
                                 STACK_MAGIC, parse_info)

EXE = HERE / "build" / "record_test.exe"
record = subprocess.run([str(EXE)], capture_output=True, check=True).stdout
assert len(record) == 64 + 12 * 1024 + 4, len(record)
assert record[:4] == STACK_MAGIC

dev = Device()
dev.info = parse_info(
    "I fw=1.2.0 proto=3 board=rpipico2 pixels=1024 bits=14 vref_mv=2500 "
    "stack=1 stack_block=16 stack_block_max=32 cap_max_frames=16384")
assert dev.info.pixels == 1024 and dev.info.stack == 1

# A frame ahead of it, so the reader has to keep both record types straight.
codes = np.arange(1024, dtype="<u2") % 16384
frame = (FRAME_MAGIC + (11).to_bytes(4, "little") + (1024).to_bytes(2, "little")
         + (1024).to_bytes(2, "little") + codes.tobytes()
         + (int(codes.sum()) & 0xFFFF).to_bytes(2, "little"))

stream = b"BOOT: hello\nA cap sending id=7 bytes=12356\n" + record + frame

buf = bytearray()
rng = np.random.default_rng(1)
pos = 0
while pos < len(stream):
    n = int(rng.integers(1, 700))
    buf.extend(stream[pos:pos + n])
    pos += n
    while dev._consume(buf):
        pass

stacks = dev.drain_stacks()
frames = dev.drain_frames()
msgs = dev.drain_messages()

assert len(stacks) == 1, (len(stacks), [m for m in msgs])
assert len(frames) == 1, len(frames)
assert dev.stack_checksum_errors == 0
assert dev.checksum_errors == 0

r = stacks[0]
assert (r.capture_id, r.dark) == (7, True)
assert (r.frames_requested, r.frames_attempted) == (150, 150)
assert (r.frames_stacked, r.frames_excluded) == (148, 2)
assert (r.exposure_us, r.duration_ms) == (66666, 10012)
assert (r.block_frames, r.sigma, r.clip_iters) == (16, 3.0, 5)
assert (r.stat_missing, r.stat_stale, r.stat_badbits) == (41, 9, 3)
assert (r.stat_max_late_us, r.stat_nostart) == (812, 1)
assert r.pixels == 1024

# Pixel p: p+1 (mod 148) samples of value p*7 mod 16384, so the mean is that
# value exactly and the deviation is zero. Pixel 3 has no samples at all.
for p in (0, 1, 2, 4, 5, 100, 512, 1023):
    n = (p % 148) + 1
    val = (p * 7) % 16384
    assert r.accepted[p] == n, (p, r.accepted[p], n)
    assert r.rejected[p] == p % 5, (p, r.rejected[p])
    assert abs(r.mean[p] - val) < 1e-3, (p, r.mean[p], val)
    assert r.sd[p] == 0.0, (p, r.sd[p])

assert r.accepted[3] == 0
assert np.isnan(r.mean[3]) and np.isnan(r.sd[3])

# The missing count is derived, not transmitted.
missing = r.frames_stacked - r.accepted - r.rejected
assert missing[3] == r.frames_stacked

# A corrupted record must be rejected, not accepted with wrong values.
bad = bytearray(record)
bad[200] ^= 0x01
dev2 = Device()
dev2.info = dev.info
b2 = bytearray(bad + b"\n")
while dev2._consume(b2):
    pass
assert dev2.stacks_received == 0, "corrupted record was accepted"
assert dev2.stack_checksum_errors >= 1

print("wire format round trip ok:", r.summary())
