"""Read a .STK file without the instrument.

Two forms exist and both are read here: the container written by firmware
1.3 and later, and the plain record written before that. See the layout
comments above sdBuildFileHeader() and buildStackHeader() in src/main.cpp.

This module was moved out of tools/read_stk.py so that the application can
open a file from a card reader through exactly the code that the standalone
reader uses. One decoder, not two.
"""

import numpy as np

from .device import (STACK_HEADER_BYTES, STACK_MAGIC, STACK_PIXEL_BYTES,
                     STACK_PIXEL_DTYPE, STACK_RECORD_VERSION, StackResult)

FILE_MAGIC = bytes((0xA5, 0x5A, 0x5D, 0x01))
FILE_HEADER_BYTES = 24
CODEC_NONE = 0
CODEC_RLE = 1
CODEC_NAMES = {CODEC_NONE: "none", CODEC_RLE: "rle"}

PIXELS = 1024
PIXEL_REGION = STACK_PIXEL_BYTES * PIXELS
RECORD_BYTES = STACK_HEADER_BYTES + PIXEL_REGION + 4


class BadFile(Exception):
    pass


def _u16(b, o):
    return int.from_bytes(b[o:o + 2], "little")


def _u32(b, o):
    return int.from_bytes(b[o:o + 4], "little")


def decode_pixels(payload, count, stride):
    """Undo the firmware codec: run-length coding over byte planes.

        <n><value>        a run of n copies of value, n from 1 to 255
        <0><n><n bytes>   n literal bytes

    Plane k holds byte k of every pixel record, so the decoded stream is
    plane 0 in full, then plane 1, and so on. Transposing it back is the
    last step.
    """
    flat = bytearray()
    i = 0
    n = len(payload)
    while i < n:
        token = payload[i]
        i += 1
        if token == 0:
            if i >= n:
                raise BadFile("literal block with no length")
            ln = payload[i]
            i += 1
            if ln == 0 or i + ln > n:
                raise BadFile("literal block runs past the end of the file")
            flat.extend(payload[i:i + ln])
            i += ln
        else:
            if i >= n:
                raise BadFile("run with no value")
            flat.extend(bytes([payload[i]]) * token)
            i += 1
        if len(flat) > count * stride:
            raise BadFile("the file decodes to more than one record")
    if len(flat) != count * stride:
        raise BadFile(f"the file decodes to {len(flat)} of "
                      f"{count * stride} pixel bytes")
    planes = np.frombuffer(bytes(flat), dtype=np.uint8).reshape(stride, count)
    return planes.T.tobytes()


def read_record(path):
    """The plain stack record held in one file, whatever form it takes.

    Returns (record, codec, stored_bytes)."""
    with open(path, "rb") as fh:
        raw = fh.read()

    if raw[:4] == STACK_MAGIC:
        # Written before compression existed.
        if len(raw) != RECORD_BYTES:
            raise BadFile(f"{len(raw)} bytes, expected {RECORD_BYTES}")
        record = raw
        codec = CODEC_NONE
    elif raw[:4] == FILE_MAGIC:
        if len(raw) < FILE_HEADER_BYTES:
            raise BadFile("truncated container header")
        version = _u16(raw, 4)
        codec = _u16(raw, 6)
        plain_bytes = _u32(raw, 8)
        stored_bytes = _u32(raw, 12)
        stride = _u16(raw, 16)
        header_bytes = _u16(raw, 18)
        stored_sum = _u32(raw, 20)
        if version != 1:
            raise BadFile(f"container version {version} is not supported")
        if plain_bytes != RECORD_BYTES or stride != STACK_PIXEL_BYTES:
            raise BadFile("the file describes a record this reader cannot "
                          f"build ({plain_bytes} bytes, stride {stride})")

        at = FILE_HEADER_BYTES
        header = raw[at:at + header_bytes]
        at += header_bytes
        payload = raw[at:at + stored_bytes]
        at += stored_bytes
        trailer = raw[at:at + 4]
        if len(header) != header_bytes or len(payload) != stored_bytes \
                or len(trailer) != 4:
            raise BadFile("the file is shorter than its own header says")
        if (sum(payload) & 0xFFFFFFFF) != stored_sum:
            raise BadFile("the stored payload failed its checksum; the card "
                          "returned damaged data")

        if codec == CODEC_NONE:
            pixels = payload
            if len(pixels) != PIXEL_REGION:
                raise BadFile("uncompressed payload of the wrong length")
        elif codec == CODEC_RLE:
            pixels = decode_pixels(payload, PIXELS, STACK_PIXEL_BYTES)
        else:
            raise BadFile(f"codec {codec} is not known to this reader")
        record = header + pixels + trailer
    else:
        raise BadFile("not a spectrum file")

    if record[:4] != STACK_MAGIC:
        raise BadFile("the record inside the file has the wrong magic")
    want = _u32(record, len(record) - 4)
    if (sum(record[:-4]) & 0xFFFFFFFF) != want:
        raise BadFile("the record failed its own checksum")
    return record, codec, len(raw)


def parse_record(record):
    """The header fields and the per-pixel arrays, as a dict.

    Same layout as Device._consume_stack; see buildStackHeader() in
    src/main.cpp."""
    version = _u16(record, 4)
    if version != STACK_RECORD_VERSION:
        raise BadFile(f"record version {version} is not supported")
    body = np.frombuffer(record[STACK_HEADER_BYTES:len(record) - 4],
                         dtype=STACK_PIXEL_DTYPE)
    return {
        "capture_id": _u32(record, 8),
        "dark": bool(_u16(record, 6) & 1),
        "frames_requested": _u32(record, 12),
        "frames_attempted": _u32(record, 16),
        "frames_stacked": _u32(record, 20),
        "frames_excluded": _u32(record, 24),
        "exposure_us": _u32(record, 28),
        "duration_ms": _u32(record, 32),
        "pixels": _u16(record, 36),
        "block_frames": _u16(record, 38),
        "sigma": _u16(record, 40) / 100.0,
        "clip_iters": _u16(record, 42),
        "stat_missing": _u32(record, 44),
        "stat_stale": _u32(record, 48),
        "stat_badbits": _u32(record, 52),
        "stat_max_late_us": _u32(record, 56),
        "stat_nostart": _u32(record, 60),
        "mean": body["mean"].astype(np.float64),
        "sd": body["sd"].astype(np.float64),
        "accepted": body["accepted"].astype(np.int32),
        "rejected": body["rejected"].astype(np.int32),
    }


def load(path):
    """One file from disk as the same StackResult a device transfer gives.

    Returns (result, codec_name, stored_bytes) so a caller can say how the
    file was stored without decoding it a second time.
    """
    record, codec, stored = read_record(path)
    d = parse_record(record)
    result = StackResult(
        capture_id=d["capture_id"],
        dark=d["dark"],
        frames_requested=d["frames_requested"],
        frames_attempted=d["frames_attempted"],
        frames_stacked=d["frames_stacked"],
        frames_excluded=d["frames_excluded"],
        exposure_us=d["exposure_us"],
        duration_ms=d["duration_ms"],
        block_frames=d["block_frames"],
        sigma=d["sigma"],
        clip_iters=d["clip_iters"],
        stat_missing=d["stat_missing"],
        stat_stale=d["stat_stale"],
        stat_badbits=d["stat_badbits"],
        stat_max_late_us=d["stat_max_late_us"],
        stat_nostart=d["stat_nostart"],
        mean=d["mean"],
        sd=d["sd"],
        accepted=d["accepted"],
        rejected=d["rejected"],
    )
    return result, CODEC_NAMES.get(codec, str(codec)), stored
