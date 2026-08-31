"""The card codec must be exactly lossless, and must refuse damaged input.

codec_test.exe runs the firmware's own coder and decoder, extracted from
src/main.cpp. This drives it with records that look like real captures, then
decodes its output again with an independent implementation written here. Two
implementations agreeing is the point: a coder tested only against its own
decoder passes even when both share a mistake.

The ratio is checked as well, but loosely. It is a property of the data, not
of the code, and a spectrum with more structure compresses better than one
with less. What would be a real fault is a file LARGER than the record it
holds, which is what run-length coding does to this record without the byte
plane transpose in front of it: measured 12607 bytes out for 12288 in.
"""
import subprocess
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "tools"))

EXE = HERE / "build" / "codec_test.exe"

PIX = 1024
STRIDE = 12
HDR = 64
PIXEL_BYTES = PIX * STRIDE
RECORD_BYTES = HDR + PIXEL_BYTES + 4
DT = np.dtype([("mean", "<f4"), ("sd", "<f4"),
               ("accepted", "<u2"), ("rejected", "<u2")])


def make_record(kind, seed):
    """A record whose float32 bits are produced the way the firmware
    produces them: mean is an integer sum divided by the accepted count, so
    the low mantissa bits carry the same noise a real capture carries. A
    record built from round numbers would compress far better than anything
    the instrument ever writes."""
    rng = np.random.default_rng(seed)
    px = np.arange(PIX)
    if kind == "peaks":
        base = 900.0 + 60.0 * np.sin(px / 130.0)
        for centre, height, width in ((210, 9000, 3.2), (455, 14000, 2.8),
                                      (700, 5200, 4.0), (890, 2400, 6.0)):
            base += height * np.exp(-0.5 * ((px - centre) / width) ** 2)
    elif kind == "dark":
        base = np.full(PIX, 780.0) + 12.0 * np.sin(px / 60.0)
    elif kind == "flat":
        base = 6000.0 * np.exp(-0.5 * ((px - 512) / 300.0) ** 2) + 400.0
    elif kind == "saturated":
        base = np.full(PIX, 16383.0)
    elif kind == "noise":
        # The worst case for any lossless coder: no structure at all.
        base = rng.uniform(0, 16383, PIX)
    else:
        base = np.zeros(PIX)

    n = np.full(PIX, 148, dtype=np.int64)
    n[rng.random(PIX) < 0.02] = 147
    total = np.rint((base + rng.normal(0.0, 3.0, PIX)) * n).astype(np.int64)
    total = np.clip(total, 0, 16383 * n)

    body = np.zeros(PIX, dtype=DT)
    body["mean"] = (total / n).astype(np.float32)
    body["sd"] = np.abs(rng.normal(3.0, 0.8, PIX)).astype(np.float32)
    body["accepted"] = n.astype(np.uint16)
    body["rejected"] = rng.integers(0, 3, PIX).astype(np.uint16)
    if kind not in ("saturated", "noise"):
        body["mean"][3] = np.nan       # a pixel that kept no sample
        body["accepted"][3] = 0

    rec = bytearray(RECORD_BYTES)
    rec[0:4] = bytes((0xA5, 0x5A, 0xC7, 0x2E))   # STACK_MAGIC
    rec[4:6] = (1).to_bytes(2, "little")         # record version
    rec[36:38] = PIX.to_bytes(2, "little")       # pixel count
    rec[HDR:HDR + PIXEL_BYTES] = body.tobytes()
    total_sum = sum(rec[:RECORD_BYTES - 4]) & 0xFFFFFFFF
    rec[RECORD_BYTES - 4:] = total_sum.to_bytes(4, "little")
    return bytes(rec)


def decode(payload):
    """Independent decoder. Same format, written from the description rather
    than from the firmware: <n><value> is a run, <0><n><bytes> is literals."""
    flat = bytearray()
    i = 0
    while i < len(payload):
        token = payload[i]
        i += 1
        if token == 0:
            ln = payload[i]
            i += 1
            flat.extend(payload[i:i + ln])
            i += ln
        else:
            flat.extend(bytes([payload[i]]) * token)
            i += 1
    # Undo the byte-plane transpose: plane k holds byte k of every pixel.
    planes = np.frombuffer(bytes(flat), dtype=np.uint8).reshape(STRIDE, PIX)
    return planes.T.tobytes()


KINDS = ["peaks", "dark", "flat", "saturated", "noise", "zeros"]
records = [make_record(k, i + 1) for i, k in enumerate(KINDS)]

run = subprocess.run([str(EXE)], input=b"".join(records), capture_output=True)
assert run.returncode == 0, run.stderr.decode(errors="replace")
print("  harness:", run.stderr.decode(errors="replace").strip())

# Split the payloads back out and decode each one here.
out = run.stdout
pos = 0
worst = 0.0
for kind, rec in zip(KINDS, records):
    ln = int.from_bytes(out[pos:pos + 4], "little")
    pos += 4
    payload = out[pos:pos + ln]
    pos += ln

    got = decode(payload)
    want = rec[HDR:HDR + PIXEL_BYTES]
    assert got == want, f"{kind}: the independent decoder disagrees"

    stored = 24 + HDR + ln + 4          # what the file actually costs
    ratio = RECORD_BYTES / stored
    print(f"    {kind:<10} {stored:>6} bytes  {ratio:5.2f}x")
    # No file may be larger than the record it holds. The firmware falls back
    # to storing plain when the coder does not help, so this bound is what
    # decides whether that fallback is ever needed.
    if kind not in ("noise",):
        assert ratio > 1.0, f"{kind}: compression made the file bigger"
    worst = max(worst, stored / RECORD_BYTES)

assert pos == len(out), "the harness wrote more payloads than records"

# Real spectra must actually gain something. 1.4 is well under the 1.6 the
# probe measured, so this fails on a broken transpose rather than on a
# spectrum that happens to be flatter than usual.
for kind, rec in zip(KINDS, records):
    if kind in ("peaks", "dark", "flat"):
        one = subprocess.run([str(EXE)], input=rec, capture_output=True)
        ln = int.from_bytes(one.stdout[0:4], "little")
        assert RECORD_BYTES / (24 + HDR + ln + 4) > 1.4, (
            f"{kind} compressed to only "
            f"{RECORD_BYTES / (24 + HDR + ln + 4):.2f}x")


def decoder_says(payload):
    r = subprocess.run([str(EXE), "decode"],
                       input=len(payload).to_bytes(4, "little") + payload,
                       capture_output=True)
    assert r.returncode == 0, r.stderr.decode(errors="replace")
    return int(r.stdout.decode().strip())


# Damaged input. These bytes come off a memory card, so a flipped bit in a
# length field must end the decode rather than run off the end of the buffer.
good_len = int.from_bytes(out[0:4], "little")
good = out[4:4 + good_len]
assert decoder_says(good) == PIXEL_BYTES, "the good payload must still decode"

assert decoder_says(good[:len(good) // 2]) == 0, \
    "a truncated payload must be rejected"
assert decoder_says(good + b"\xff\x00") == 0, \
    "trailing bytes past a complete image must be rejected"
assert decoder_says(b"\x00") == 0, "a literal header with no length"
assert decoder_says(b"\x00\x05ab") == 0, "a literal claiming more than it has"
assert decoder_says(b"\xff") == 0, "a run header with no value"
assert decoder_says(b"\x00\x00") == 0, "a zero length literal block"
assert decoder_says(b"") == 0, "an empty payload is not a whole image"
# A run that would overrun the pixel array by one byte.
assert decoder_says(b"\xff\x41" * ((PIXEL_BYTES // 255) + 1)) == 0, \
    "a payload longer than the pixel array must be rejected"

# A whole card file, assembled by the firmware's own header builder, read by
# the tool somebody uses when they have the card but not the instrument.
# Without this the reader is only ever tested against a file the test itself
# wrote, which proves nothing about the firmware.
import read_stk  # noqa: E402  (tools/ is on the path from the header above)

tmp = HERE / "build"
for kind, rec in zip(KINDS, records):
    made = subprocess.run([str(EXE), "file"], input=rec, capture_output=True)
    assert made.returncode == 0, made.stderr.decode(errors="replace")
    path = tmp / f"card_{kind}.STK"
    path.write_bytes(made.stdout)

    record, codec, stored = read_stk.read_record(path)
    assert record == rec, f"{kind}: read_stk rebuilt a different record"
    assert stored == len(made.stdout)
    d = read_stk.parse_record(record)
    assert d["pixels"] == PIX

# A plain record on a card, as firmware before 1.3 wrote it, must still read.
legacy = tmp / "card_legacy.STK"
legacy.write_bytes(records[0])
record, codec, stored = read_stk.read_record(legacy)
assert record == records[0] and codec == 0, "a pre-container file must read"

# And damage must be reported rather than returned as data.
bad = bytearray((tmp / "card_peaks.STK").read_bytes())
bad[FILE_HEADER := 24 + 40] ^= 0xFF
(tmp / "card_bad.STK").write_bytes(bytes(bad))
try:
    read_stk.read_record(tmp / "card_bad.STK")
    raise AssertionError("a corrupted payload was accepted")
except read_stk.BadFile:
    pass

print("codec ok: lossless on every case, every damaged payload refused, "
      "and read_stk.py reads what the firmware writes")
