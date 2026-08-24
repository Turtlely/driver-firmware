# Architecture Reference

This document describes the firmware in `src/main.cpp` and the PIO program
in `src/pixel_trig.pio`.

Companion documents:

| Document | Contents |
|---|---|
| `APPLICATION.md` | Operating the spectrometer application, and host code design |
| `ISSUES.md` | Every defect found in this project, its cause and its resolution |

`ISSUES.md` is the more useful starting point before changing the capture
path. Most of the obvious simplifications in this design have already been
tried and have already failed for reasons that are not obvious.

## 1. Scope

The firmware runs on a Raspberry Pi Pico 2 (RP2350). Its function is to:

1. Trigger a sensor scan on an external CPLD driver board.
2. Capture 1024 pixel values per scan from an ADS7279 14-bit ADC.
3. Transmit each frame to a host computer over USB serial (CDC).

The Pico does not generate sensor timing. The CPLD does. The Pico's only
output to the CPLD is a single START pulse per frame.

## 2. Hardware interface

### 2.1 Pin assignment

| Signal | GPIO | Physical pin | Direction | Function |
|---|---|---|---|---|
| `PIN_START` | 7 | 10 | Pico output | Starts one CPLD scan cycle |
| `PIN_PIXEL_TRIG` | 1 | 2 | Pico input | ADS7279 `~INT` / EOC output |
| `PIN_ADC_SCLK` | 10 | 14 | Pico output | ADS7279 SPI clock, bit-banged |
| `PIN_ADC_MISO` | 8 | 11 | Pico input | ADS7279 SDO (result data) |
| `PIN_ADC_MOSI` | 11 | 15 | Pico output | ADS7279 SDI (command data) |
| `PIN_ADC_CS` | 9 | 12 | Pico output | ADS7279 chip select |

The ADS7279 operates in a separate voltage domain from the Pico and is
connected through an external bidirectional level shifter. The level
shifter has been measured to behave asymmetrically: the SDI-related path
becomes unreliable at SPI clock rates above roughly 200 kHz, while the
MISO path is suspected of a slow low-to-high transition consistent with
an RC-limited pull-up. This is a hardware characteristic of the level
shifter, not of the ADS7279 or the Pico, and constrains the SPI bit
timing chosen in firmware (Section 4.2).

### 2.2 ADS7279 command protocol

The ADS7279 SPI interface uses a 4-bit command field followed by 12 bits
of data, for a 16-bit total frame.

| Command | Bits | Meaning |
|---|---|---|
| `1100` | Read CFR | Returns the 12-bit configuration register in the low 12 bits of the response |
| `1110` | Write CFR | Writes 12 bits of data to the configuration register |
| `1111` | Default mode | Achieved by holding SDI high; performs a conversion read with no command interpretation |

Firmware holds `PIN_ADC_MOSI` high during normal per-pixel reads, which
sends command `1111` on every transaction. This is the ADS7279's
documented default-mode behavior and requires no command decoding on the
ADC's part for a plain conversion readout.

### 2.3 Configuration register (CFR) bit map

| Bit | Field |
|---|---|
| D11 | Channel select |
| D10 | CCLK source |
| D9 | Trigger mode (1 = manual, via CONVST) |
| D8 | Don't care |
| D7 | EOC/`~INT` polarity |
| D6 | Pin 10 function |
| D5 | Chain mode vs. EOC/`~INT` output |
| D4 | Auto-nap |
| D3 | Nap power-down |
| D2 | Deep power-down |
| D1 | TAG (always reads 0 on this part) |
| D0 | Reset (0 = reset asserted, 1 = normal operation; does not self-clear) |

Firmware writes the CFR twice at boot, in `resetAdcConfig()`: once with
D0 = 0 to force a known reset state, then once with D0 = 1 to return the
part to normal operation. D0 does not self-clear. A firmware or wiring
fault that leaves D0 at 0 produces every conversion reading as `0x3FFF`,
which is the documented in-reset output and was previously observed and
diagnosed as this exact condition.

CFR write status is a known open item. See Section 9.1.

## 3. SPI bit-banging

### 3.1 Why bit-banged, not hardware SPI

The RP2350 hardware SPI peripheral drives a continuous or block-based
clock relative to its own internal timing. This firmware requires the
opposite: a clock generated fresh, on demand, only in direct response to
a confirmed `~INT` event, with no independent free-running relationship
to any other clock in the system.

An earlier version of this firmware used a free-running PIO-generated
SPI clock. That design produced a slow drifting artifact in captured
frames, traced to the free-running clock's phase drifting against the
CPLD's independently clocked pixel timing. Two clocks that are each
individually stable but not phase-locked to each other will always drift
relative to one another over time. Generating the clock fresh for each
transfer removes this failure mode by construction: each transfer's
timing is self-contained and does not depend on when, in absolute time,
it happens to start.

### 3.2 Bit timing

`readAdcSample()` and `transferAdc16()` in `main.cpp` implement the SPI
transfer. Both mask interrupts for the full 16-bit transfer using
`save_and_disable_interrupts()`, so no other interrupt source (USB,
system tick) can stretch the bit timing.

The inter-edge delay is implemented in `adcClockDelay()` as four
unrolled NOP instructions, approximately 27 ns at 150 MHz. This value
was reached empirically:

- A 6-NOP delay (~40 ns) was the original value.
- Widened to 24 NOPs (~160 ns) to test whether the level shifter's rise
  time was corrupting bits read too soon after a transition. This did
  not reduce the observed error rate.
- Narrowed to 4 NOPs to test the opposite hypothesis: shorter transfers
  reduce the time interrupts are masked per pixel, reducing the chance
  that a read overlaps unrelated line activity near the next trigger.

The ADS7279 itself tolerates any SPI clock period from 20 ns to 2 us, so
timing precision on the ADS7279's behalf is not the constraint. The
level shifter is the limiting factor, and its exact behavior at the
chosen timing has not been confirmed with an oscilloscope.

### 3.3 Result decoding

The ADS7279 returns a 16-bit word. The 14-bit conversion result occupies
the upper 14 bits; the lower 2 bits are discarded. Firmware extracts the
result as `(sample >> 2) & 0x3FFF`.

## 4. Pixel trigger detection

This is the most heavily revised part of the firmware and merits full
explanation, including the two approaches that were tried and rejected.

### 4.1 Requirement

The CPLD produces exactly 1024 `~INT` falling edges per scan. This is
fixed by the CPLD design and treated as ground truth. Firmware must
capture exactly one ADC read per genuine edge: no more, no fewer.

`~INT` is not a clean digital signal. Measurement showed a companion
glitch approximately 1 to 2 us after each genuine falling edge,
consistent with contact bounce or a coupled transient on the trigger
line as it passes through the level shifter. The true inter-pixel
interval is approximately 20 us. Any edge-detection design must reject
this glitch without rejecting the next genuine edge, inside that 20 us
budget.

### 4.2 Rejected approach 1: ARM-core interrupt

The first working design used `attachInterrupt()` on a falling edge,
with a software state machine (`ARMED` / `REFRACTORY`): a falling edge
in the `ARMED` state is accepted and read immediately; the state then
moves to `REFRACTORY` for a fixed window (`REFRACTORY_US`, 16 us), during
which further edges are ignored outright.

This design has a hardware limitation independent of the software logic.
The RP2350's GPIO edge-detect circuitry latches a pending interrupt as a
single sticky bit per pin per edge type, not as a counter. If two
falling edges (a genuine edge and its companion glitch, or two genuine
edges) occur closer together than the CPU's interrupt-service latency,
the second edge does not generate a second interrupt. It coalesces into
the first. The result is a silently missed pixel, with no trace in any
software counter, because there is nothing for the software to reject.

### 4.3 Rejected approach 2: CPU polling

The second design removed the interrupt entirely and replaced it with a
tight busy-poll loop reading the raw GPIO level directly, modeled on a
sibling project's sensor driver that uses this technique successfully
for its own timing-critical signal.

This was measured to perform worse than the interrupt-based design. The
sibling project's signal is generated by its own PIO block and is
electrically clean; polling works because nothing else needs to preempt
observation of it. `~INT` in this system is an external signal that must
be observed continuously to avoid loss, and the RP2350 continues to
service other interrupt sources (USB stack activity in particular) even
with the pixel-trigger interrupt removed. Each of those interrupts
preempts the poll loop for its service duration. Unlike a hardware edge
latch, a raw level read has no memory of a transition that occurred
entirely during a preemption. A narrow enough pulse, occurring entirely
inside one such preemption window, is lost with no trace at all. This
failure mode is strictly worse than interrupt coalescing, because it
requires no coincidence of two edges, only one preemption of sufficient
length at the wrong moment.

### 4.4 Accepted approach: PIO hardware debounce

`src/pixel_trig.pio` implements the same `ARMED` / `REFRACTORY` policy as
Section 4.2, but entirely inside a PIO state machine, independent of the
ARM core's interrupt controller. This removes both failure modes:

- A PIO state machine cannot be preempted by ARM-core interrupts. It
  runs on its own clock, continuously, regardless of what the CPU is
  doing.
- Confirmed triggers are reported through the state machine's RX FIFO,
  configured to a joined depth of 8 words, not a single sticky bit. A
  CPU stall of up to several pixel periods is absorbed by the FIFO
  rather than causing loss.

Program logic, in order:

1. Wait for the trigger pin to go low. This is the candidate pixel
   trigger.
2. Push one word to the RX FIFO immediately. The word's value carries no
   information; only its presence signals one confirmed trigger.
3. Hold a refractory count, in cycles, equal to `REFRACTORY_US`. During
   this count, the pin is not examined at all. Any glitch during this
   window is invisible to the program by construction, not filtered
   after detection.
4. After the refractory count expires, check whether the pin is already
   high. If so, re-arm immediately (return to step 1).
5. If not, poll for up to a fixed bound (20 us) waiting for the pin to
   go high, then re-arm regardless of outcome.

Step 5 exists because of a defect found in the first version of this
program, which waited for the pin to go high with no bound. That design
is correct when the trigger line returns high promptly after its
refractory window, but if it ever fails to do so, for any reason, the
state machine blocks indefinitely. No further pixels are captured for
the remainder of that scan. This was observed on hardware as an
intermittent, large drop in captured pixel count on isolated frames
(1024 falling to approximately 800), with normal operation resuming on
the following frame once the line eventually did go high and the state
machine naturally recovered. Bounding the wait converts an unbounded
stall into a bounded delay with a small, defined worst case: a single
possible extra spurious trigger if a pulse's low period exceeds the
refractory window and the bound simultaneously, which is a minor and
rare error, not a scan-ending fault.

### 4.5 State machine clock rate

A PIO `wait` instruction samples its pin once per state machine cycle.
The state machine's clock divider is therefore the edge-detection
sampling rate, not merely a timing convenience.

Earlier revisions set the divider so one PIO cycle equalled one
microsecond, chosen only so `REFRACTORY_US` could be pushed in as a cycle
count with no unit conversion. The side effect was that the state machine
observed `~INT` at 1 MHz and was blind between samples. A pulse narrower
than the sampling interval, or one opening and closing between two
samples, was never observed at all. This is the same class of blind
window that CPU polling was rejected for in Section 4.3, reintroduced by
a different route: moving observation off the ARM core prevents
preemption, but throttling the observer to 1 MHz restores the defect.

The state machine now runs at the full system clock (divider 1.0,
approximately 6.7 ns per cycle at 150 MHz), sampling the pin roughly 150
times per microsecond. The refractory window is consequently counted in
cycles rather than microseconds. `main.cpp` converts `REFRACTORY_US` to a
cycle count at runtime from the measured system clock frequency, so
`REFRACTORY_US` remains the single source of truth in wall-clock terms.

The diagnostic `raw_edge_count` state machine (Section 4.7) runs at the
same full rate. A diagnostic sampling more coarsely than the mechanism it
audits would report its own blind windows as genuine signal loss.

### 4.6 Runtime configuration

`REFRACTORY_US` is defined once in `main.cpp` and pushed into the PIO
state machine at initialization using the `pull noblock` idiom: a value
written to the state machine's TX FIFO before it starts is loaded once
and persists in the `X` register indefinitely, since nothing pushes a
replacement afterward. `main.cpp` remains the single source of truth for
this value; the `.pio` source requires no manual synchronization.

The state machine's clock divider is set so that one PIO cycle equals
one microsecond, computed from the actual system clock frequency at
runtime via `clock_get_hz(clk_sys)`. This allows `REFRACTORY_US` to be
used directly as a cycle count with no unit conversion.

### 4.7 Raw edge count (diagnostic)

Moving debounce into PIO removed a capability the earlier software
design had: a count of edges rejected as bounce. `pixel_trig.pio` cannot
report this, by design; it never observes the pin during its refractory
window at all, so there is nothing for it to count.

`raw_edge_count`, a second, minimal PIO program in the same file, runs on
its own state machine watching the same pin with no filtering. It pushes
one FIFO word per low-to-high transition, unconditionally, including
bounce. `runScan()` drains it every iteration and reports the total as
`raw` in the diagnostic line (Section 7.2).

Comparing `raw` against `pulses` for the same frame distinguishes two
otherwise indistinguishable failure modes:

- `raw` well above 1024 while `pulses` is short: `REFRACTORY_US` is
  discarding genuine pixels, not just bounce. It should be reduced.
- `raw` is itself short of 1024: pulses are being lost before either
  state machine observes them. This is not a debounce tuning problem and
  points at the signal path itself (wiring, level shifter, GPIO
  configuration).

### 4.8 Build step and stale-header protection

`pixel_trig.pio` is not compiled directly. It is assembled into
`pixel_trig.pio.h` by the `pioasm` tool bundled with the PlatformIO
RP2040/RP2350 platform package, and that generated header is what the
firmware actually includes.

This arrangement has a specific hazard. PlatformIO's Arduino builder has
no equivalent of the Pico SDK's `pico_generate_pio_header()` CMake rule,
so historically the header was regenerated by hand. Editing a `.pio` file
and forgetting that step produces firmware that compiles cleanly, links
cleanly, flashes cleanly, and silently contains the previous version of
the PIO program. Nothing in the build output indicates this. The sibling
mainboard project documented losing significant debugging time to exactly
this failure, evaluating timing theories against a binary that never
contained the edits under test.

Two independent protections are in place:

1. `tools/build_pio.py` is registered in `platformio.ini` as a pre-build
   action. It regenerates any `src/*.pio.h` whose `.pio` source is newer,
   before every build. A syntax error in a `.pio` file fails the build
   rather than leaving the old header in place.
2. `printPioBuildStamp()` in `main.cpp` prints, at boot, the instruction
   count and a checksum computed over the PIO instruction words actually
   linked into the running binary, together with the clock divider and
   refractory cycle count actually programmed into the state machine.

The second exists because the first proves only what the build system
intended. The checksum is computed from the linked program image, so it is
positive confirmation of what is executing on the hardware. If
`pixel_trig.pio` is modified and the boot checksum does not change, the
running firmware does not contain the modification.

Manual regeneration remains available:

```
pioasm src/pixel_trig.pio src/pixel_trig.pio.h
```

## 5. Frame capture sequence

`runFrame()` executes once per frame, called from `loop()` when the
scheduled frame time (`g_nextFrameStartUs`) has arrived. It performs, in
order:

1. Clear both PIO state machine FIFOs, discarding anything left over
   from a previously abandoned scan (Section 6).
2. Assert `PIN_START` for `START_PULSE_WIDTH_US` (5 us), then deassert.
   This is the only signal the CPLD receives from the Pico. It is issued
   as early as possible in the function to keep frame timing independent
   of anything that follows.
3. Call `runScan()`, which blocks until the pixel buffer is filled or a
   watchdog timeout elapses (Section 4, Section 5.1, Section 6).
4. Format and stage the frame's text for transmission (Section 8).
5. Schedule the next frame's start time.

There is no separate tail-fill step. `runScan()` pre-fills the entire
buffer with the `MISSING_PIXEL` sentinel before capture begins, so every
slot not written by a genuine trigger is already marked, wherever it
falls.

Frame capture is fully synchronous. There is no interrupt service
routine filling a buffer in the background. A single pixel buffer is
used, not double-buffered, because there is no concurrent writer: by the
time `runFrame()` proceeds to the next scan, the current frame's data has
already been copied into the transmission staging buffer.

### 5.1 Positional indexing

A pixel's slot in the buffer is derived from when its trigger arrived,
not from how many triggers have been counted so far. The two disagree
precisely when a pixel is lost, and that disagreement was the mechanism
behind the drifting image.

Under simple append-in-arrival-order storage, one lost pixel mid-scan
shifts every subsequent pixel in that frame one slot earlier. The frame
remains internally plausible, with no gap and no marker, but its entire
tail is spatially wrong by an amount that varies frame to frame with
where and how often loss occurs. Rendered live, that presents as an image
sliding sideways.

Each accepted trigger's index is instead the previous index advanced by
the number of pixel periods that actually elapsed:

```
steps = round(gap / pixel_period)
index = previous_index + steps
```

A normal gap yields `steps == 1`. A gap of about two periods yields
`steps == 2`, placing the pixel in its true slot and leaving the skipped
slot holding `MISSING_PIXEL`. Later pixels are unaffected. Loss becomes a
localized, explicitly marked hole rather than a global shift.

The calculation is relative to the previous trigger rather than to the
scan start, so error in the period estimate cannot accumulate across a
frame. Each decision is local.

**False positives are as harmful as the original defect.** Inserting a
hole that did not occur shifts every later pixel by one, reproducing the
drift this scheme exists to prevent. The timestamp is taken when the CPU
pops the FIFO, which is not when the trigger arrived; those diverge
whenever the CPU falls behind and the PIO queues triggers in the
meantime. A gap inflated purely by CPU scheduling must not be read as
loss.

The FIFO makes this distinction reliable. Its entries are strictly
consecutive confirmed triggers, and a pixel cannot be lost between two
entries already queued in it. Therefore:

- FIFO level greater than 1: the CPU is draining a backlog, the timestamp
  is not meaningful, and the correct step is exactly 1.
- FIFO level of 1: the queue had drained and the CPU was genuinely
  waiting for this trigger, so the timestamp is an accurate arrival time
  and the gap is real evidence.

Loss is only inferred in the second case, and only when the gap reaches
at least 1.5 periods. `MAX_INDEX_JUMP` (8) caps how far any single gap
may advance the index, so one anomalous measurement produces a short
frame rather than flinging the remaining pixels to the end of the buffer.

### 5.2 Pixel period estimation

The pixel period is measured rather than assumed. `g_pixelPeriodUs` is
refreshed at the end of each frame from the **median** of that frame's
trustworthy gaps, computed from a 64-bucket histogram at 1 us resolution
and clamped to a plausible range. `PIXEL_PERIOD_NOMINAL_US` (20) seeds it
before any frame has been measured.

The choice of statistic is not cosmetic. An earlier implementation used
the **minimum** gap, reasoning that a missed pixel can only make a gap
larger, so the smallest gap should be the cleanest sample of the period.
That reasoning ignores measurement jitter in the opposite direction.
Timestamps are taken when the CPU pops the FIFO, and `readAdcSample()`
blocks with interrupts disabled for 6 to 9 us, so a trigger arriving
mid-read is observed late and the following one then appears
artificially close.

Measured on hardware, against a true period of 20.0 us:

| Quantity | Observed |
|---|---|
| Frame duration / 1024 | 20.01 us, stable on every frame |
| Per-frame gap range | 13 to 26 us |
| Minimum-gap estimate | 13 to 19 us, varying frame to frame |
| Median-gap estimate | 20 us, every frame |

The consequence of the minimum-gap estimate was severe and self-
inflicted. With the estimate at 13 us, the loss threshold
(`period + period/2`) fell to 19.5 us, so an ordinary 20 us gap crossed
it and scored two steps. Every pixel then advanced two slots. One
captured frame recorded `pulses 513 miss 511 dur 10273us`: exactly half a
frame, in exactly half the time, with the entire scan consumed by holes
the firmware had invented. Loss detection was generating the drift it was
written to prevent.

The median is robust to jitter in both directions and remains correct
with a substantial fraction of genuine holes present, since holes appear
only in the upper tail. Verified against the measured jitter
distribution: the median returns exactly 20 us even with 200 of 1023 gaps
spanning a genuine hole, while the minimum returns 11 us.

Two further guards keep bad samples out of the estimator:

- Only gaps that scored exactly one step contribute. A gap spanning a
  genuine hole would bias the estimate upward.
- Only gaps observed while genuinely idle-waiting contribute. The
  `wasIdleWaiting` flag is set when the capture loop observes an empty
  FIFO and cleared on every pop, so a trigger that queued while
  `readAdcSample()` held interrupts off is excluded. FIFO level alone
  cannot distinguish that case, since such a trigger still presents at
  level 1.

## 6. Scan watchdog

`runScan()` is a blocking loop. If the CPLD stops producing triggers
entirely, mid-scan, nothing in the PIO program or the polling loop would
otherwise terminate it. `SCAN_WATCHDOG_US` (100 ms, approximately five
times the expected ~20.5 ms scan duration) bounds this. If exceeded,
`runScan()` returns with whatever pixel count it reached and
`timedOut = true`, reported as `wdog 1` in the diagnostic line
(Section 7.2).

A watchdog timeout means firmware stopped waiting, not that the CPLD
stopped sending. The CPLD has no way to know the timeout occurred and
may continue producing trigger pulses for the remainder of that scan on
its own schedule. Both PIO state machines (Section 4.4, 4.7) continue
accepting and queuing these into their FIFOs, since neither has any
concept of a software-side timeout. `runFrame()` clears both FIFOs
immediately before firing each START pulse, discarding any such stale
entries. Without this, the next frame's `runScan()` would pop leftover
entries from the abandoned scan as if they were its own first pixels,
corrupting and misaligning that frame too and turning one short frame
into a cascade of them.

### 6.1 Scans that never begin, and START retry

The whole-scan watchdog is the wrong instrument for detecting a scan that
never started. Measured on hardware, the CPLD occasionally does not act on
a START pulse at all, at a rate of roughly one frame in several hundred.

The signature is unambiguous:

```
S 3067 pulses 0 raw 0 miss 1024 wdog 1 fifo 0 gap 0-0us dur 100022us
```

`raw 0` is what makes it conclusive. The unfiltered diagnostic state
machine (Section 4.7) saw zero transitions, so nothing was emitted at all,
as opposed to something being emitted and rejected by debounce. `dur`
equals `SCAN_WATCHDOG_US` exactly.

Handled by the whole-scan watchdog alone, one such event cost far more
than a single frame. The capture loop blocked for the full 100 ms, so the
frame was lost and the following frame ran approximately 34 ms late,
disrupting the schedule well beyond the original fault.

`FIRST_TRIGGER_TIMEOUT_US` (2 ms) bounds the first trigger specifically.
The first pixel of a healthy scan arrives within tens of microseconds of
START, so the absence of any trigger after 2 ms is conclusive rather than
merely suspicious. On that condition `runScan()` returns immediately with
`neverStarted` set, and `runFrame()` clears both FIFOs and issues another
START, up to `START_RETRY_LIMIT` (3) additional attempts.

Re-issuing START is safe precisely because the CPLD demonstrably did
nothing with the previous pulse and is therefore idle. The retry limit
means a genuinely disconnected CPLD degrades to one short frame per frame
period rather than spinning.

Measured over 1053 frames after this change: two START-ignored events
occurred, both recovered on the first retry, each producing a complete
1024-pixel frame at a cost of approximately 2 ms of additional scan time.
Maximum frame lateness across the run fell from 33762 us to 2 us.

## 7. Serial wire protocol

All output uses a single USB CDC serial port at 115200 baud (baud rate
is not meaningful for USB CDC but is set for compatibility with terminal
tools). Two line types are emitted per frame, plus unstructured boot
text.

### 7.1 Frame (binary, protocol 2)

```
uint8[4]      magic  A5 5A D5 1C
uint32        sequence
uint16        captured count
uint16        pixel count
uint16[count] pixel data
uint16        checksum, sum of data words modulo 65536
```

Little-endian throughout. 2062 bytes for 1024 pixels.

Frames were hex ASCII in protocol 1, at two characters per data byte.
That made transmission the frame rate limit rather than the sensor: the
achievable ceiling was about 30 fps against a 48.8 Hz scan limit, and
above it the host fell behind far enough to lose pixels. Binary halves
the cost and moves the limit back to the scan.

The magic can occur inside pixel data by chance, so a reader must
validate the pixel count and the checksum before accepting a frame.
Both matching makes a false frame essentially impossible while still
allowing resynchronisation after a dropped byte.

- `frame_seq`: monotonically increasing frame counter, starts at 0.
- `captured_count`: number of pixels genuinely received this frame
  (0 to 1024). This is a count, not a position: with positional indexing
  (Section 5.1), missing pixels may fall anywhere in the frame, so this
  value does not indicate where valid data ends.
- Hex data: exactly 4096 uppercase hex characters, 4 characters per
  pixel, 1024 pixels, most significant nibble first. A genuine pixel is
  a 14-bit code in the range `0000` to `3FFF`. The value `FFFF` is the
  `MISSING_PIXEL` sentinel, indicating no trigger arrived for that slot.

`FFFF` is deliberately outside the 14-bit code range so it is
unambiguous; a real conversion cannot produce it. This replaces an
earlier zero-fill convention, which was indistinguishable from a genuine
0 mV reading and only worked at all under the assumption that losses
formed a contiguous tail.

The line is written into a single contiguous buffer before any of it is
transmitted (Section 8), so it cannot be interleaved with other output
on the wire.

### 7.2 Frame diagnostic line

```
S <frame_seq> pulses <n> raw <n> miss <n> period <n>us wdog <0|1> nostart <0|1> retry <n> early <n> fifo <n> gap <min>-<max>us late <n>us dur <n>us maxread <n>us txdrop <n> px<idx> <code> (0x<hex>, <mV> mV)
```

| Field | Meaning |
|---|---|
| `pulses` | Same as `captured_count` in the data line: pixels genuinely received |
| `raw` | Total unfiltered transitions seen this frame by the diagnostic `raw_edge_count` state machine, bounce included (Section 4.7) |
| `miss` | Slots left as `MISSING_PIXEL` this frame. `pulses + miss` should equal 1024 |
| `period` | Current measured pixel period in microseconds, used for positional indexing (Section 5.1) |
| `wdog` | 1 if the whole-scan watchdog fired (Section 6), else 0 |
| `nostart` | 1 if the scan still had not begun after all START retries were exhausted (Section 6.1) |
| `retry` | Additional START pulses issued this frame because the CPLD ignored the first. Nonzero with `nostart 0` means the retry recovered the frame |
| `early` | Count of reads where `~INT` was already high again at the end of the 16-bit SPI transfer, meaning the trigger pulse was shorter than the read itself (Section 9.2) |
| `fifo` | Largest PIO RX FIFO backlog observed during the scan; a value approaching 8 indicates the CPU is close to falling behind the PIO's confirmed-trigger rate |
| `gap` | Minimum and maximum interval, in microseconds, between consecutive accepted triggers this frame |
| `late` | How overdue this frame's start was relative to its own schedule; should remain small and stable |
| `dur` | Total wall-clock duration of this frame's `runFrame()` call, dominated by the scan itself (approximately 20 ms under normal operation) |
| `maxread` | Longest single 16-bit SPI transfer this frame, in microseconds |
| `txdrop` | Running total of frames whose text was skipped because the previous frame had not finished transmitting (Section 8) |
| `px<idx>` | The 1-based pixel index configured by `REPORT_PIXEL_INDEX` (500 by default), followed by its decimal code, hex code, and millivolt equivalent |

This line is informational only. `tools/live_view.py` ignores it.

### 7.3 Boot text

Plain ASCII lines beginning with `BOOT:` are printed once at startup,
including CFR readback values from `resetAdcConfig()`. `setup()` blocks
on `while (!Serial)` before printing anything, so a terminal opened after
reset still sees the full boot sequence; USB CDC on this platform does
not report itself as ready until a host asserts DTR.

## 8. Non-blocking transmission

Frame text (Section 7.1 and 7.2, concatenated) is written into a single
staging buffer, `g_txStaging`, by `runFrame()`. `runFrame()` never calls a
blocking write. Bytes are drained from the staging buffer by `pumpTx()`,
which checks `Serial.availableForWrite()` and copies at most that many
bytes, then returns immediately regardless of how much remains. `pumpTx()`
is called from `loop()` on every iteration and repeatedly from inside
`runScan()`'s capture loop, so a frame's data continues draining
throughout the following frame's ~20 ms scan.

This design exists because of a specific measured fault. An earlier
version issued a large (~4 KB) blocking `Serial.write()` directly from
the frame-assembly function, before firing the START pulse for the next
frame. If the host is slow to drain the USB CDC endpoint, for any
reason, including host-side activity with no relationship to the sensor
(observed with both a trackpad click and multiple open windows), the
write blocks for longer, which delays the START pulse, which perturbs
frame timing. The non-blocking design removes this coupling entirely:
frame timing depends only on bounded CPU work, never on host behavior.

If a frame's text has not fully drained by the time the next frame is
ready to stage its own text, the new frame's text is dropped rather than
overwriting a buffer still being read from, or blocking to wait.
`live_view.py` already renders whichever complete frame it most recently
received, so a dropped frame is a skipped display update, not corrupted
data. Each drop increments `g_txDroppedFrames`, reported as `txdrop` in
the diagnostic line.

## 9. Known open items

### 9.1 CFR write verification

`resetAdcConfig()` writes the CFR and reads it back, expecting `0xFFD`.
The readback does not match, and more importantly it returns a
**different value on different boots**: `0xFFC` on one, `0x100` on
another, with the write sequence and firmware identical between them.
Both boots produced clean conversion data.

A register that reads back inconsistently while the device operates
correctly indicates the readback path itself is unreliable, not that the
register holds an unexpected value. This is consistent with the level
shifter limitation on the SDI-related path (Section 2.1), supported by
earlier testing showing CFR readback stable at 200 kHz SPI clock and
random at 2 MHz. It has not been confirmed with an oscilloscope at the
ADS7279 pins.

This does not affect capture. Per-pixel reads use default-mode command
`1111`, achieved by holding SDI high, and never consult the CFR. The
practical consequence is that the boot-time CFR lines are not a
trustworthy diagnostic and should not be used to infer chip state.

### 9.2 Early trigger deassertion

`readAdcSample()` checks whether `~INT` is still asserted low at the end
of its own 16-bit transfer. If it has already gone high, the transfer ran
against a trigger pulse shorter than the read itself. This is tracked as
`early` in the diagnostic line (Section 7.2).

Measured, this counter is **saturated**: it fires on essentially every
read, 945,151 of 945,152 across one capture. The `~INT` pulse is simply
shorter than the roughly 6 us bit-banged transfer on this hardware, as a
normal and constant condition rather than an intermittent fault.

This is benign for data integrity. The ADS7279 holds the conversion
result on DOUT until the next conversion begins, so a read that outlives
the `~INT` pulse still clocks out valid data, which the measured pixel
values confirm.

It does mean the counter has no discriminating power as written, since a
value of 1024 per frame is the normal case. Its remaining diagnostic use
is inverted: a value that ever drops meaningfully below the pixel count
would indicate the pulse width changed, which would be worth
investigating.

The original motivating hypothesis, that a SAR conversion read too early
would bias toward full scale, is not supported. Saturation at 100 percent
alongside clean, stable, physically sensible pixel values is inconsistent
with the reads being corrupted.

### 9.3 Isolated pixel value outliers (spikes)

With capture integrity resolved, frames arrive complete and correctly
ordered but individual pixels sometimes carry badly wrong values, visible
as sharp single-pixel excursions both above and below the local signal
level. These are distinct from missing pixels: `miss` reads 0, so every
pixel was captured and placed correctly, and only the value is wrong.

`tools/live_view.py` classifies these automatically (`analyze_spikes`).
The method rests on two observations:

1. A real optical feature cannot be one pixel wide. The point spread
   function blurs any scene detail across several pixels, so genuine
   structure appears as a step or a broad bump. An isolated sample that
   differs sharply from both neighbours is electrical in origin.
2. A single misread bit in the SPI transfer perturbs the code by exactly
   a power of two. Analog causes, meaning incomplete settling, reference
   droop, or noise, perturb it by an arbitrary amount.

The viewer therefore measures how tightly outlier amplitudes cluster on
powers of two. Because powers of two are spaced one octave apart, a
tolerance band catches arbitrary amplitudes by chance at a known rate:
`POW2_CHANCE_RATE`, approximately 17 percent at the 6 percent tolerance
used. The verdict compares the observed rate against that baseline rather
than against an arbitrary threshold, and reports both.

Validated against simulated data with known cause:

| Simulated cause | Observed power-of-2 rate | Verdict |
|---|---|---|
| Single-bit errors on bits 11-13 | 100 percent | power-of-2 dominant |
| Arbitrary analog excursions | 13 percent | not power-of-2, analog |
| Clean signal | no outliers detected | none |
| Broad optical features | 0 percent, 5 samples | too few to judge |

An earlier version used an 18 percent tolerance, which covers more than
half an octave and misattributed 39 percent of simulated analog noise to
bit errors. Tolerance width matters more than it appears.

If the verdict reports power-of-2 dominance, the offending bit is named
and the cause is in the SPI readout path, where the level shifter
(Section 2.1) is the leading suspect. If it reports analog, the cause is
upstream of the Pico entirely and relates to Section 9.4.

### 9.4 Fixed pattern interference at 17.68 kHz

Resolved. Recorded here because the wrong explanation stood for some time
and the way it was corrected is the useful part.

Captured frames carry a ripple on what should be a smooth signal. It was
originally described as broad waviness spanning tens to hundreds of
pixels, and attributed to one of two mechanisms: incomplete settling of
the analog video line before the ADC's sample instant, or reference and
supply droop under a sustained run of bright pixels.

Both were wrong, and both were rejected by the same measurement. Each
predicts an error that scales with signal level. The ripple's amplitude
is constant at 54 codes whether the pixel sits at 2780 or 2345 codes, so
it is additive and unrelated to how far the video line had to move. A
direct test of the settling hypothesis, correlating the disturbance
against the step size from the preceding pixel, also came back negative.

The actual cause is a single sinusoid at 0.3539 cycles per pixel, 2.825
pixels per period, which at the 20 microsecond pixel period is 17.68 kHz.
The "broad waviness" was moire: a 2.8 pixel period drawn across a plot
about 1000 pixels wide beats against the display grid and renders as slow
undulation. The apparent slow drift across the pixel axis, and its
dependence on frame rate, was the beat between this fixed interferer and
the frame rate, which was one of the two hypotheses considered at the
time. `miss` is zero throughout, so positional indexing (Section 5.1) had
already excluded the capture path as required.

The interferer is asynchronous to the readout: 50 kHz / 17.68 kHz =
2.825, not an integer, so its phase relative to frame start is uniformly
random. This is why it evaded detection for so long. Frame to frame, a
17.68 kHz component aliases into apparent randomness, and it was
repeatedly measured as ordinary noise. It is only visible along the
readout axis, where the effective sampling rate is 50 kHz rather than the
frame rate. Spatial autocorrelation of the within-frame residual reaches
+0.89 and -0.89 at small lags.

Corrected in host software rather than firmware, by
`processing.notch_pattern()`. The analog sample is latched before the
Pico's capture logic runs, so firmware cannot affect it. See ISSUES.md
entry 31 for the measurements, the rejected hypotheses, and the fidelity
verification. The source itself is upstream of the Pico and has not been
identified; the Pico's own regulator was tested and ruled out.

## 10. Build and flash

Toolchain: PlatformIO, `env:pico2`, `platform =
https://github.com/maxgerhardt/platform-raspberrypi.git`, `board =
rpipico2`, `framework = arduino`.

```
pio run
```

produces `.pio/build/pico2/firmware.uf2`. Flash by holding BOOTSEL while
connecting the Pico, or via the platform's standard upload target.

If `src/pixel_trig.pio` has been edited, regenerate its header first
(Section 4.6). A build will succeed silently on a stale header with no
error, so this step cannot be skipped.

## 11. Host software

The operator-facing application is documented separately in
`APPLICATION.md`, which covers both operation and the design of the host
code. This section records only the parts that constrain firmware.

### 11.1 Command protocol

The device accepts plain text commands on the same port the frame stream
uses, handled in `loop()` between frames so a command cannot perturb pixel
timing inside a scan.

| Command | Response |
|---|---|
| `id` | `I` line with device identification |
| `rate <hz>` | `A rate_hz=<n>` |
| unrecognised | `E unknown_command=<text>` |

The `I` line is emitted once at boot and on request. It is a contract with
the host application: adding key=value fields is safe, renaming or
reordering existing ones is not, and no value may contain a space because
the line is space-separated.

Fields: `fw`, `board`, `pixels`, `bits`, `vref_mv`, `rate_hz`, `rate_min`,
`rate_max`, `period_us`, `refractory_us`, `sysclk_mhz`, `pio_crc`,
`missing_code`, `built`.

`pio_crc` is the checksum described in Section 4.8. Exposing it to the
host makes a stale generated header visible from the application rather
than only in the boot log.

### 11.2 Frame rate

`rate` sets the START interval, which is the exposure control. It is
bounded at 1 to 40 Hz. The upper bound exists because a scan occupies
about 20.5 ms; above roughly 45 Hz the next START would be scheduled
before the current scan completed.

### 11.3 Legacy tool

`tools/live_view.py` remains as a minimal viewer with the shift and drift
diagnostics used during development. It is not the operator interface.

## 12. Revision history

This section records major design changes to the pixel capture path, in
order, for engineers who need to understand why the current design looks
the way it does rather than a simpler alternative.

1. Initial bring-up: hardware SPI, fixed pin mapping corrections.
2. Bit-banged SPI adopted to allow on-demand clock generation.
3. Free-running PIO SPI clock adopted for higher throughput, later
   identified as the cause of a slow drifting artifact (Section 3.1) and
   replaced with on-demand bit-banging.
4. Pixel trigger debounce implemented as a software `ARMED` /
   `REFRACTORY` state machine driven by `attachInterrupt()` (Section
   4.2).
5. Non-blocking serial transmission staging introduced after diagnosing
   USB write blocking as the cause of frame timing perturbation
   correlated with unrelated host activity (Section 8).
6. Pixel trigger detection moved to CPU busy-polling, to address
   suspected interrupt coalescing; measured to perform worse (Section
   4.3) and superseded.
7. Pixel trigger detection moved to a PIO hardware state machine
   (Section 4.4), combining immunity to ARM-core interrupt preemption
   with FIFO-based tolerance to brief CPU stalls.
8. PIO program corrected to bound its post-refractory wait for the
   trigger line to return high, after an unbounded wait was found to
   cause intermittent large mid-scan capture stalls (Section 4.4).
9. FIFO clearing added before each START pulse, so triggers queued after
   an abandoned scan cannot be consumed as the next frame's first pixels
   (Section 6).
10. Diagnostic `raw_edge_count` state machine added to distinguish
    debounce over-rejection from loss occurring before debounce
    (Section 4.7).
11. PIO state machine clock divider corrected from 1 MHz to full system
    clock, after the divider was identified as throttling pin sampling
    and reintroducing blind windows equivalent to the CPU-polling defect
    (Section 4.5). This is the most likely root cause of the steady
    pixel loss that revisions 7 and 8 did not resolve.
12. Positional indexing adopted, deriving each pixel's buffer slot from
    arrival time rather than arrival order, so that any remaining pixel
    loss produces a marked hole instead of shifting the rest of the frame
    (Section 5.1).
13. Automated PIO header regeneration (`tools/build_pio.py`) and a
    boot-time PIO checksum (`printPioBuildStamp()`) added, so a stale
    generated header cannot silently invalidate testing (Section 4.8).
14. Pixel period estimator changed from minimum gap to median gap, after
    hardware capture showed the minimum tracking measurement jitter
    rather than the period and causing the firmware to fabricate holes
    (Section 5.2). This was the dominant remaining cause of residual
    drift and of intermittent half-length frames.
15. Diagnostics monitor integrated into `tools/live_view.py`, since
    exclusive COM port ownership prevented running a separate terminal
    alongside the viewer (Section 11).

16. START retry added, after a 322-frame capture showed the CPLD
    occasionally ignoring a START pulse entirely, costing both the frame
    and roughly 34 ms of schedule disruption (Section 6.1).
17. Final drain of the diagnostic state machine's FIFO added after the
    capture loop exits, correcting a persistent off-by-one in `raw` that
    was an artifact of when observation stopped rather than a real
    discrepancy between the two state machines.

### Measured result

A 923-frame capture over 61.7 seconds through `live_view.py`, sensor
illuminated, represents the current state:

| Metric | Value |
|---|---|
| Frame rate | 14.97 fps against a 15 Hz target |
| Frames lost | 0 (`txdrop` 0, wire 0) |
| Missing pixels | 1 of 945,152 |
| That one pixel | frame 0, the first scan after boot |
| Missing pixels excluding frame 0 | 0 of 944,128 |
| Period estimate | 20 us, no variation |
| Maximum frame lateness | 0 us |
| Watchdog timeouts | 0 |
| Unrecovered START failures | 0 (1 retry issued, recovered) |
| Peak FIFO backlog | 2 of 8 |

The capture path is considered solved. Remaining known issues (Section 9)
are analog or diagnostic, not pixel-integrity.

An earlier 1053-frame capture over 70 seconds, taken during development
of the START retry, recorded:

| Metric | Before (322 frames) | After (1053 frames) |
|---|---|---|
| Frames at full 1024 pixels | 99.3 percent | 100.00 percent |
| Total missing pixels | 1027 | 0 |
| Frames containing holes | 4 | 0 |
| Scans that never began | 1 | 0 unrecovered (2 recovered by retry) |
| Watchdog timeouts | 1 | 0 |
| Maximum frame lateness | 33762 us | 2 us |
| Period estimate | 20 us | 20 us |
| `raw` equal to `pulses` | approximately half of frames | 1053 of 1053 |

The 1027 missing pixels in the earlier capture decompose as 1024 from a
single scan that never began, plus 3 isolated single-pixel losses across
the remaining 321 frames.

Two START-ignored events occurred during the later run and both recovered
on the first retry, each yielding a complete 1024-pixel frame.

Earlier still, before the clock divider and period estimator were
corrected, the same measurements showed the period estimate varying
between 13 and 19 us, frames as short as 513 pixels, and up to 511
missing pixels in a single frame.
