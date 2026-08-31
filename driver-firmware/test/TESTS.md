# Stacked capture checks

    python test/run_tests.py

Four checks. Two of them compile firmware code for this computer; `run_tests.py`
extracts that code out of `src/main.cpp` on every run rather than keeping a
copy, so a check cannot pass against a stale copy of a function that has since
changed. That is the same failure the generated PIO header had (`ISSUES.md`
entry 25), and it is worth designing out rather than remembering.

They need `g++` on PATH. Without one the two arithmetic checks are skipped, and
the script says so rather than reporting a clean run.

| Check | What it establishes |
|---|---|
| `clip_compare.py` | `stackClipPixel()` gives the right answer. 4000 randomised pixels, with doubled samples, halved samples and missing slots, at 1 to 10 sigma. |
| `record_check.py` | The result record survives the wire. Built by the firmware's own `buildStackHeader`/`buildStackPixels`, delivered in ragged chunks alongside a frame, parsed by `device.py`. |
| `tx_interleave_check.py` | Nothing can be written into the middle of a result record. Runs the real queue and the real sender against a fake port while diagnostic text tries to get out. |
| `live_regress.py` | The live stream still works. Frames, `S`, `A`, `E` and `I` lines mixed together, including a frame whose pixel data contains both record magics by chance. |
| `histogram_check.py` | The Distribution histogram shades bars by the sigma band they fall in, splitting a bar that straddles a boundary. Checked on a Gaussian pixel, an arcsine one, and one carrying doubled samples. |
| `app_flow_check.py` | One capture end to end through the real application, from the panel to the displayed spectrum and the saved file. |

## What clip_compare compares against

Two references, and the difference between them is the point.

`exact_clip()` in that file is the same algorithm in Python `Fraction`s, with no
rounding anywhere. The firmware does its whole threshold test in `int64`, so it
should match this exactly, and it does: **0 mismatches in 3996 clipped pixels.**

`processing.sigma_clip_combine()` is the implementation the application uses,
in `float64`. The firmware disagrees with it on **2 of 3996**, and both are
samples sitting exactly on the threshold, where the host is the one that is
wrong: its deviation lands a few units in the last place short of the exact
value and rejects a sample that belongs. Those two are printed every run. If
that count moves, or if a mismatch appears that is not a boundary tie, the
firmware has changed behaviour and not merely rounding.

An earlier version of the firmware refused to clip below four surviving
samples. That guard changed the answer on 75 of 3611 pixels and was removed;
see the comment in `stackClipPixel()` for why a floor there is the wrong
protection.

## Why tx_interleave_check exists

It is the check for a failure that shipped and had to be found on hardware.

A frame is staged with one `txAppend` and cannot be divided. A result record
can: it is three times the size of the queue and goes out a chunk at a time
over many `loop()` passes. `reportFilteredTriggerCount()` wrote about 110
bytes on every frame, in every mode, so those bytes landed inside the record.
The host read a record of the wrong length, failed its checksum,
resynchronised by one byte and never completed it. The window sat on
"collecting result".

None of the other checks could see it, because they all hand the record to the
host directly instead of through the queue that both writers share.

Take the `g_txBinaryLock` test out of `txLine()` and this check fails the way
the instrument did: 54 lines get in, the stream grows from 12396 to 16425
bytes, and the host reports a checksum failure. That negative result is worth
reproducing if the transmit path is ever reworked.
