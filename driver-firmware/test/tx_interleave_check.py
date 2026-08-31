"""Nothing may be written into the middle of a result record.

This is the check for the failure that shipped. The result record is fed into
the transmit queue in 384 byte chunks over many loop() passes, while
reportFilteredTriggerCount() writes about 110 bytes on every frame. Those
bytes landed inside the record, the host read a record of the wrong length,
and the capture hung at "collecting result".

tx_test.exe runs the real queue and the real sender against a fake port, with
text trying to get out during the transfer. This parses what the port
received: it has to be the text before the transfer, then one clean record,
then the text after, and nothing in between.
"""
import subprocess
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "tools"))
from spectrometer.device import Device, STACK_MAGIC, parse_info  # noqa: E402

EXE = HERE / "build" / "tx_test.exe"

run = subprocess.run([str(EXE)], capture_output=True)
assert run.returncode == 0, ("the sender never finished: " +
                             run.stderr.decode(errors="replace"))
wire = run.stdout
print("  harness:", run.stderr.decode(errors="replace").strip())

start = wire.find(STACK_MAGIC)
assert start != -1, "no record was sent at all"
record_len = 64 + 12 * 1024 + 4
end = start + record_len

before = wire[:start]
after = wire[end:]

# The record must be one unbroken run. Any text inside it would have to
# contain printable ASCII runs, but the real test is that the host accepts it.
dev = Device()
dev.info = parse_info("I fw=1.2.0 proto=3 board=rpipico2 pixels=1024 "
                      "bits=14 vref_mv=2500 stack=1")
buf = bytearray()
rng = np.random.default_rng(7)
pos = 0
while pos < len(wire):
    n = int(rng.integers(1, 500))
    buf.extend(wire[pos:pos + n])
    pos += n
    while dev._consume(buf):
        pass

stacks = dev.drain_stacks()
assert dev.stack_checksum_errors == 0, (
    f"{dev.stack_checksum_errors} checksum failures: something was written "
    f"into the record")
assert len(stacks) == 1, f"expected one record, host accepted {len(stacks)}"

r = stacks[0]
assert r.capture_id == 7 and r.frames_stacked == 148
assert abs(r.mean[500] - (500 * 7 % 16384)) < 1e-3
assert np.isnan(r.mean[3])

# Text before and after must survive. Text during must not appear at all: it
# is dropped, deliberately, rather than deferred.
msgs = [t for _, t in dev.drain_messages()]
joined = " ".join(msgs)
assert "cap sending" in joined, "the acknowledgement before the transfer was lost"
assert "live=1" in joined, "the lock leaked: text after the transfer was lost"
assert "T frame" not in joined, (
    "a per-frame diagnostic line escaped during the transfer; it would have "
    "landed inside the record")
assert b"T frame" not in wire[start:end], "text found inside the record bytes"

# The binary frame path, which is the one that shipped broken. runFrame()
# stages the frame itself, not only its status line, and that append used to
# test the free space alone. A capture leaves the device QUIET and stages no
# frames, so a result transfer never met it; the live view keeps running
# while somebody browses the card, so every "sd get" did.
assert b"FRAMEBEG" in wire[:start], (
    "the harness never staged a frame at all, so the check below proves "
    "nothing")
assert b"FRAMEBEG" not in wire[start:end], (
    "a binary frame was staged during the transfer and landed inside the "
    "record")

print(f"one clean record, {len(before)} bytes before and {len(after)} after, "
      f"nothing inside")
