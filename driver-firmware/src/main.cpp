#include <Arduino.h>
#include <hardware/gpio.h>
#include <hardware/sync.h>
#include <hardware/pio.h>
#include <hardware/clocks.h>
#include <hardware/timer.h>
#include <hardware/structs/sio.h>
#include <pico/platform.h>
#include "pixel_trig.pio.h"

constexpr uint8_t PIN_START = 7; // Pico physical pin 10 = GPIO 7
constexpr uint8_t PIN_PIXEL_TRIG = 1; // physical pin 2
constexpr uint8_t PIN_ADC_SCLK = 10; // physical pin 14 (SPI1 SCK)
constexpr uint8_t PIN_ADC_MISO = 8; // physical pin 11 (SPI1 RX)
constexpr uint8_t PIN_ADC_MOSI = 11; // physical pin 15 (SPI1 TX)
constexpr uint8_t PIN_ADC_CS = 9; // physical pin 12

constexpr uint32_t START_PULSE_WIDTH_US = 5;
constexpr uint32_t FRAME_RATE_HZ = 15;
constexpr uint32_t FRAME_PERIOD_US = 1000000UL / FRAME_RATE_HZ;
constexpr uint32_t PIXEL_COUNT = 1024; // CPLD pulses CONVST exactly this many times per frame
// Measured, not assumed: the reject/accept counters and the min raw gap
// (back when this ran in software) showed real edges paired with a
// companion glitch only ~1-2 us later - closer to contact bounce on ~INT
// than a delayed self-coupled glitch from our own CS transition. True
// inter-pixel interval is ~20 us. REFRACTORY_US sits well clear of the
// observed ~2 us bounce while leaving most of the ~20 us window as margin
// before the next real edge - this is the ONE number the PIO debounce
// program (pixel_trig.pio) is built around. It's pushed into the PIO SM at
// runtime (see initPixelTrigPio()), so this is the single source of truth -
// nothing in the .pio file needs hand-editing to match it.
//
// Widened from 8 to 16 (back when this was a software window) after a
// saturated (should-be-flat-white) test scene showed a dense, fast
// comb-noise pattern that got visibly cleaner at 16 - confirming 8 us
// wasn't quite enough margin to reliably absorb the bounce every time.
constexpr uint32_t REFRACTORY_US = 16;

constexpr uint16_t ADC_CODE_MASK = 0x3FFF; // 14-bit result, left-justified on the wire

// Written into any pixel slot whose trigger never arrived. Deliberately
// OUTSIDE the 14-bit code range (max 0x3FFF) so it is unambiguous on the
// wire: live_view.py treats any value above ADC_CODE_MASK as "no data",
// which a real reading can never produce. Previously a short frame was
// zero-filled, which is indistinguishable from a genuine 0 mV reading and
// only worked at all because losses were assumed to be a contiguous tail.
constexpr uint16_t MISSING_PIXEL = 0xFFFF;

// Nominal CPLD pixel cadence, used as the initial estimate for positional
// indexing before any frame has been measured. The live value is measured
// per frame and adapts (see g_pixelPeriodUs), so this only has to be in
// the right neighbourhood.
constexpr uint32_t PIXEL_PERIOD_NOMINAL_US = 20;
constexpr uint32_t PIXEL_PERIOD_MIN_US = 4;   // clamp: reject implausible measurements
constexpr uint32_t PIXEL_PERIOD_MAX_US = 200; // clamp: reject implausible measurements

// Ceiling on how far a single gap may advance the write index. A gap long
// enough to imply more than this many skipped pixels is far more likely to
// be a stall, a scheduling artifact, or a measurement glitch than that
// many genuinely lost pixels in one run. Capping it bounds the damage: the
// frame ends up short rather than having its remaining pixels flung to the
// end of the buffer, where they would be both wrong and unrecoverable.
constexpr uint32_t MAX_INDEX_JUMP = 8;

// Current best estimate of the CPLD's true pixel period, refreshed at the
// end of every frame from the MEDIAN of that frame's observed gaps.
//
// The median is essential, and an earlier version got this badly wrong by
// using the MINIMUM gap instead. The reasoning behind the minimum was that
// a missed pixel can only make a gap larger, never smaller, so the
// smallest gap should be the cleanest sample of the period. That ignores
// measurement jitter in the other direction. Timestamps are taken when the
// CPU pops the FIFO, and readAdcSample() blocks with interrupts disabled
// for 6-9 us, so a trigger arriving mid-read is observed late; the
// following one then appears artificially close. On hardware this produced
// gaps spanning 13-26 us against a true period of 20.0 us, and the minimum
// tracked the low end of that jitter rather than the period.
//
// The consequence was severe and self-inflicted. With the estimate at
// 13 us, an ordinary 20 us gap exceeded the 1.5x loss threshold and scored
// 2 steps, so EVERY pixel advanced two slots. One captured frame showed
// pulses 513 / miss 511 / dur 10273us: exactly half a frame in exactly
// half the time, the whole scan consumed by holes the firmware invented.
// Loss detection was manufacturing the drift it was written to prevent.
//
// The median is robust to jitter in both directions and still correct with
// a substantial fraction of genuine holes present, since holes only appear
// in the upper tail. Cross-check: frame duration divided by PIXEL_COUNT
// gave 20.01 us on every frame captured, matching the median and not the
// minimum.
uint32_t g_pixelPeriodUs = PIXEL_PERIOD_NOMINAL_US;

// Gap histogram used to compute that median. One 1 us bucket per index;
// gaps at or beyond the top are clamped into the last bucket, which is
// harmless because anything that long is far above the period and only
// ever sits in the upper tail.
constexpr uint32_t GAP_HIST_BUCKETS = 64;
constexpr uint32_t GAP_HIST_MIN_SAMPLES = 64; // below this, a frame is too sparse to re-estimate from
constexpr uint32_t REPORT_PIXEL_INDEX = 500; // 1-based ordinal reported each frame
constexpr uint32_t ADC_VREF_MV = 2500; // REF5025 reference
constexpr uint32_t ADC_FULL_SCALE = 1u << 14; // 14-bit converter

// Single buffer, not double: capture is fully synchronous now (see
// runScan()/runFrame() below), so there's no ISR racing the main loop for
// access to it any more. By the time runFrame() moves on to the next
// scan, this frame's pixel data has already been copied out into
// g_txStaging, so overwriting this buffer for the next scan is safe.
uint16_t g_pixelBuf[PIXEL_COUNT];

// Frame rate is settable at runtime because exposure is set by the START
// interval, so this is the primary acquisition control the host needs.
uint32_t g_frameRateHz = FRAME_RATE_HZ;
uint32_t g_framePeriodUs = FRAME_PERIOD_US;
constexpr uint32_t FRAME_RATE_MIN_HZ = 1; // plus 0, meaning free running

// Integration time is the START-to-START interval, so it is the frame
// period. It is settable directly as well as via a rate in hertz, because
// integer hertz is coarse where it matters most: 1 and 2 Hz are 1000 and
// 500 ms with nothing between, and at the fast end 40 and 41 Hz differ by
// only 0.6 ms. Microseconds give useful resolution across the whole range.
//
// The lower bound is the scan itself. 1024 pixels at about 20 us is
// 20.5 ms, and asking for less than that cannot be honoured: START would
// fire while the previous scan was still running. 20600 us is just above
// it, which permits experimentation while making the impossible
// unrequestable. Pixels are measurably lost below about 22700 us
// (44 Hz); 25000 us (40 Hz) is the fastest setting that captures every
// pixel.
constexpr uint32_t FRAME_PERIOD_MIN_US = 20600;
constexpr uint32_t FRAME_PERIOD_MAX_US = 10000000; // 10 s, for weak signals
// The scan is the hard floor: 1024 pixels at ~20 us is ~20.5 ms, which is
// 48.8 Hz. Requesting more than that is harmless rather than an error, as
// the schedule is simply always overdue and frames run back to back, so
// the ceiling is set above the floor to make that reachable. Transmission
// no longer binds: it limited the rate to about 30 fps under the previous
// hex-ASCII protocol and now costs roughly half as much (see
// stageBinaryFrame).
//
// Rate 0 means free running: START is re-issued as soon as the previous
// frame completes, which yields the maximum the hardware allows without
// having to guess the right number.
constexpr uint32_t FRAME_RATE_MAX_HZ = 60;

uint32_t g_nextFrameStartUs = 0;
uint32_t g_frameSequence = 0;

// Set by readAdcSample(), consumed by runScan() right after each read.
bool g_lastReadSawEarlyDeassert = false;

// The two bits of the 16-bit transfer that the 14-bit result does not use
// (readAdcSample() returns bits 15..2). If the ADS7279 drives these to a
// constant value, they are a per-read integrity check: a transfer that
// gained or lost a clock shifts them along with everything else, so a
// deviation identifies a corrupt read at the moment it happens rather than
// afterwards by comparing against neighbouring pixels.
//
// This is a measurement, not an assumption. If the histogram below shows a
// single dominant value with the exceptions matching the observed shift
// rate, the check is usable and firmware can act on it. If the low bits
// are simply noise, it is not, and this gets removed.
uint8_t g_lastReadLowBits = 0;

// --- Non-blocking outbound serial staging ---
// runFrame() must never block on Serial I/O. Composing this frame's text
// is bounded CPU work (no I/O wait); actual bytes trickle out via
// pumpTx(), gated on Serial.availableForWrite() so a call there can never
// block either - worst case a slow host just means a slower drain and a
// dropped frame or two, never a stalled capture loop. Called from loop()
// every iteration, and repeatedly from inside runScan()'s poll loop too,
// so the previous frame keeps draining throughout the ~20 ms a scan takes.
// Single output queue with atomic appends.
//
// Everything transmitted goes through here: binary frames, status lines,
// command replies. That is the point. Two writers on one stream cannot be
// made safe by timing, only by construction.
//
// The first attempt used a single frame buffer plus direct Serial.println()
// for command replies, which interleaved a reply into the middle of a frame
// and destroyed both. The second attempt suppressed command handling
// whenever the frame buffer held data, which fixed the interleaving and
// introduced a worse fault: with frames running back to back the buffer is
// never observed empty at the point commands are polled, so the device
// stopped responding to commands entirely and could not be recovered
// without a power cycle.
//
// A queue removes the conflict rather than scheduling around it. A message
// is either appended whole or rejected whole, so a partially written
// message cannot exist, and pumpTx() is the only writer to the port.
constexpr size_t TX_CAPACITY = 2 * PIXEL_COUNT + 2048;
uint8_t g_txBuf[TX_CAPACITY];
size_t g_txHead = 0; // next write position
size_t g_txTail = 0; // next byte to transmit

size_t txPending() { return g_txHead - g_txTail; }

// Free space, after accounting for the compaction that append performs.
size_t txFree() { return TX_CAPACITY - txPending(); }

// Appends a complete message, or nothing at all. Never partial.
bool txAppend(const void *data, size_t len) {
  if (len > txFree()) {
    return false;
  }
  if (g_txHead + len > TX_CAPACITY) {
    // Reclaim the space already transmitted by sliding the remainder down.
    memmove(g_txBuf, g_txBuf + g_txTail, txPending());
    g_txHead = txPending();
    g_txTail = 0;
  }
  memcpy(g_txBuf + g_txHead, data, len);
  g_txHead += len;
  return true;
}
uint32_t g_txDroppedFrames = 0; // frames whose text was skipped because the previous frame's text hadn't fully drained yet

void pumpTx() {
  if (txPending() == 0) {
    g_txHead = g_txTail = 0; // drained: reset to the start of the buffer
    return;
  }
  const int avail = Serial.availableForWrite();
  if (avail <= 0) {
    return;
  }
  size_t chunk = txPending();
  if (chunk > (size_t)avail) {
    chunk = (size_t)avail;
  }
  Serial.write(g_txBuf + g_txTail, chunk);
  g_txTail += chunk;
  if (txPending() == 0) {
    g_txHead = g_txTail = 0;
  }
}

// Queues one ASCII line, appending the newline. Used for status, replies
// and boot text so that nothing writes to Serial directly.
bool txLine(const char *text) {
  const size_t n = strlen(text);
  if (n + 1 > txFree()) {
    return false;
  }
  char buf[320];
  const size_t take = (n < sizeof(buf) - 2) ? n : sizeof(buf) - 2;
  memcpy(buf, text, take);
  buf[take] = '\n';
  return txAppend(buf, take + 1);
}

// Formats "D <frame> <pulse_count> <4096 hex chars>\n" into buf, returns
// bytes written. 4 uppercase hex digits per pixel (14-bit code,
// 0000-3FFF) - see live_view.py's frame parser for the wire format.
// Binary frame, protocol 2. Replaces a hex-ASCII line that cost two
// characters per data byte.
//
// Transmission was the frame rate limit, not the sensor. Measured on the
// ASCII protocol: requested rates above 25 Hz were not achieved, the rate
// ceiling was about 30 fps, and above that the host fell behind badly
// enough that pixels were lost outright (15 per second at a requested 40
// Hz). The scan itself takes 20.5 ms, so the hardware allows 48.8 Hz.
//
//   ASCII   4111 byte frame + 250 byte status = 209 KB/s at 48 Hz
//   binary  2062 byte frame + 250 byte status = 111 KB/s at 48 Hz
//
// Layout, little-endian throughout:
//
//   uint8[4]      magic
//   uint32        sequence
//   uint16        captured count
//   uint16        pixel count
//   uint16[count] pixel data, 14-bit codes, 0xFFFF for missing
//   uint16        checksum, sum of the data words modulo 65536
//
// The magic could in principle occur inside pixel data, so the reader
// validates the pixel count and the checksum before accepting a frame.
// Both must match, which makes a false frame essentially impossible while
// still allowing resynchronisation after a dropped byte.
//
// Status lines remain ASCII. They are small, they are read by people, and
// the reader distinguishes them by scanning for the magic.
constexpr uint8_t FRAME_MAGIC[4] = {0xA5, 0x5A, 0xD5, 0x1C};
constexpr size_t FRAME_BINARY_BYTES = sizeof(FRAME_MAGIC) + 4 + 2 + 2
                                      + (2 * PIXEL_COUNT) + 2;

size_t stageBinaryFrame(char *buf, uint32_t seq, uint32_t capturedCount,
                        const uint16_t *pixels) {
  uint8_t *p = (uint8_t *)buf;

  memcpy(p, FRAME_MAGIC, sizeof(FRAME_MAGIC));
  p += sizeof(FRAME_MAGIC);

  // Written byte by byte rather than by casting a struct over the buffer:
  // this is explicit about endianness and does not depend on the compiler
  // agreeing about alignment or padding.
  *p++ = (uint8_t)(seq & 0xFF);
  *p++ = (uint8_t)((seq >> 8) & 0xFF);
  *p++ = (uint8_t)((seq >> 16) & 0xFF);
  *p++ = (uint8_t)((seq >> 24) & 0xFF);

  *p++ = (uint8_t)(capturedCount & 0xFF);
  *p++ = (uint8_t)((capturedCount >> 8) & 0xFF);

  *p++ = (uint8_t)(PIXEL_COUNT & 0xFF);
  *p++ = (uint8_t)((PIXEL_COUNT >> 8) & 0xFF);

  uint16_t checksum = 0;
  for (uint32_t i = 0; i < PIXEL_COUNT; ++i) {
    const uint16_t v = pixels[i];
    checksum = (uint16_t)(checksum + v);
    *p++ = (uint8_t)(v & 0xFF);
    *p++ = (uint8_t)((v >> 8) & 0xFF);
  }

  *p++ = (uint8_t)(checksum & 0xFF);
  *p++ = (uint8_t)((checksum >> 8) & 0xFF);

  return (size_t)((char *)p - buf);
}

// Approximate, uncalibrated busy-wait - NOT tied to an exact frequency.
// The ADS7279 tolerates any SCLK cycle from 20 ns to 2 us, with only an
// 8 ns min high/low time, so precision on ITS behalf doesn't matter.
// Currently 4 NOPs (~27 ns/edge at 150 MHz) - low single-digit us for the
// whole 16-bit transfer, nowhere near the ~20 us per-pixel budget.
void __not_in_flash_func(adcClockDelay)() {
  // A fixed, unrolled run of NOPs - NOT a loop with a volatile counter.
  // A volatile-controlled loop forces a real memory load/store/compare
  // every iteration, and its actual duration is compiler/optimization
  // dependent rather than a known number of cycles - that's what caused
  // an earlier version of this transfer to run far slower than intended
  // and start missing pixels. A straight-line sequence has no such
  // uncertainty: at 150 MHz, each NOP is ~6.7 ns.
  //
  // 4 NOPs, roughly 27 ns per edge.
  //
  // Slowing this down was tried and rejected on measurement, not on
  // reasoning. The hypothesis was that an SCLK near 18 MHz through a level
  // shifter known to be marginal above roughly 200 kHz on the SDI channel
  // was producing ringing edges that the ADC counted twice, shifting the
  // result. Widening to 30 NOPs, an SCLK near 2.5 MHz, moved the observed
  // shift rate only from 1.33 percent to 1.03 percent, which is within
  // noise on seven events, while pushing the worst-case read from 14 us to
  // 50 us against a 20 us per-pixel budget and causing pixels to be missed
  // outright. The cost was real and the benefit was not, so the transfer
  // stays fast.
  __asm__ volatile("nop\n\t nop\n\t nop\n\t nop\n\t");
}

// Setup time between CS falling and the FIRST SCLK rising edge, which is
// the edge that samples the MSB.
//
// This is deliberately far longer than the rest of the bit timing, because
// it was measured to be the failure point. The datasheet allows tD2 <= 13
// ns for the MSB to appear on SDO after CS falls, and an earlier version
// allowed one adcClockDelay() here, roughly 27 ns, on that basis. That
// specification describes the ADS7279's own output driver and says nothing
// about the level shifter between it and this MCU (Section 2.1 of the
// architecture doc), whose propagation delay is both longer and less
// symmetric.
//
// The symptom was unambiguous once measured. Isolated pixels came back at
// exactly twice or exactly half their true value: 0x17A5 read as 0x2F4A,
// 0x17AA read as 0x0BD5. Those are exact single-bit shifts of the correct
// result, not corrupted bits, so the transfer had gained or lost one clock
// relative to the data stream. Sampling before the MSB has propagated
// yields a stale bit ahead of the real data and shifts everything right by
// one, halving the value. Missing the MSB entirely shifts left by one and
// doubles it.
//
// A value scaled by two is not a corrupted bit pattern in any obvious way,
// which is why this survived so long: a spike-amplitude analysis looking
// for single-bit flips (a fixed power-of-two error) classifies these as
// analog noise, because the error here is proportional to the signal
// rather than fixed. See analyze_spikes() in tools/live_view.py.
//
// Measured outcome: widening this from ~27 ns to 2 us moved the shift rate
// from roughly 2.8 percent to 1.3 percent, but the effect did not hold up
// under further testing, where the rate stayed near 1 percent regardless.
// It is kept at 1 us as cheap insurance against a genuine setup violation,
// not as the fix. The actual recovery is readAdcSample()'s validity check
// below. Do not raise this further without watching maxread: at 2 us the
// worst-case read reached 55 us against a 20 us per-pixel budget and
// pixels were lost.
// Implemented as a NOP block rather than busy_wait_us_32(). The library
// call rounds up to the next timer tick and adds call overhead, so a
// nominal 1 us cost 8 us of the per-pixel budget in practice and a nominal
// 2 us pushed the worst-case read to 55 us against a 20 us budget, losing
// pixels outright.
//
// Sized at ~270 ns on measurement. Doubling it to ~533 ns was tested and
// did NOT reduce the shifted-read rate (0.2776 percent versus 0.2952
// percent over ~618,000 pixels each, the longer delay marginally worse),
// while costing 56 more missed pixels per 600 frames. An earlier
// conclusion that a longer setup helped was drawn from one to three events
// on a single monitored pixel and did not survive full-frame measurement.
// CS setup time is therefore not the mechanism behind the shifts; this is
// kept short to protect the per-pixel budget.
void __not_in_flash_func(adcCsSetupDelay)() {
  __asm__ volatile(
      "nop\n\t nop\n\t nop\n\t nop\n\t nop\n\t nop\n\t nop\n\t nop\n\t"
      "nop\n\t nop\n\t nop\n\t nop\n\t nop\n\t nop\n\t nop\n\t nop\n\t"
      "nop\n\t nop\n\t nop\n\t nop\n\t nop\n\t nop\n\t nop\n\t nop\n\t"
      "nop\n\t nop\n\t nop\n\t nop\n\t nop\n\t nop\n\t nop\n\t nop\n\t"
      "nop\n\t nop\n\t nop\n\t nop\n\t nop\n\t nop\n\t nop\n\t nop\n\t");
}

// One raw 16-bit transfer. Callers should use readAdcSample(), which adds
// the validity check and recovery.
//
// SCLK is generated fresh, bit-banged, for THIS transfer alone - not a
// continuously free-running clock, so every read gets identical,
// self-contained timing no matter when in absolute time it starts.
uint16_t __not_in_flash_func(readAdcSampleOnce)() {
  uint16_t sample = 0;

  // ADS7279 SPI frame (datasheet Figure 4): the MSB is on SDO as soon as
  // CS falls (tD2 <= 13 ns); each next bit shifts out on each FALLING
  // SCLK edge. IRQs stay masked so nothing (USB IRQ, systick, ...) can
  // stretch this loop across an edge and corrupt the bit timing.
  const uint32_t irqState = save_and_disable_interrupts();

  // Direct SIO register access, not gpio_put(). Measured: the library call
  // overhead dominated this loop entirely - the worst-case read sat at 11 us
  // minimum against a 20 us per-pixel budget, with the NOP delays
  // contributing almost nothing by comparison. That also invalidates any
  // reasoning about the effective SCLK frequency based on the NOP counts
  // alone; the real clock was several times slower than intended.
  constexpr uint32_t SCLK_MASK = 1u << PIN_ADC_SCLK;
  constexpr uint32_t MISO_MASK = 1u << PIN_ADC_MISO;

  sio_hw->gpio_clr = SCLK_MASK;
  sio_hw->gpio_clr = (1u << PIN_ADC_CS); // MSB drives SDO now
  adcCsSetupDelay();                      // settle before the first sampling edge

  for (int bit = 0; bit < 16; ++bit) {
    sio_hw->gpio_set = SCLK_MASK; // rising edge: current bit is stable, sample it
    adcClockDelay();
    sample = (sample << 1) | ((sio_hw->gpio_in & MISO_MASK) ? 1u : 0u);
    sio_hw->gpio_clr = SCLK_MASK; // falling edge: next bit shifts out
    adcClockDelay();
  }

  // Diagnostic: was ~INT (PIN_PIXEL_TRIG, the same net as the ADC's
  // EOC/~INT output) still low at the end of the transfer we just trusted?
  // If it's back high already, the transfer ran against a trigger pulse
  // shorter than our own read - see the comment on earlyDeassertEdges in
  // runScan() below.
  g_lastReadSawEarlyDeassert = gpio_get(PIN_PIXEL_TRIG);
  g_lastReadLowBits = (uint8_t)(sample & 0x3); // see g_lastReadLowBits

  // The loop exits just past the 16th falling edge, satisfying t7
  // (16th falling edge >= 10 ns before CS rises).
  gpio_put(PIN_ADC_CS, true);

  restore_interrupts(irqState);

  return sample;
}

// Reads one pixel, validating the result and retrying once if it is
// provably corrupt.
//
// The check exploits a hardware invariant rather than any assumption about
// what the value "should" look like. The 14-bit result occupies bits 15..2
// of the transfer; the low two bits are not part of it, and measurement
// over 617,444 reads showed them to be zero 99.737 percent of the time.
// They are therefore a constant, and a transfer that gained or lost a
// clock shifts them along with the data, making them nonzero.
//
// That matters because the failure being caught here is otherwise
// invisible in a single read. A shifted result is a perfectly plausible
// pixel value, exactly twice or half the true one, with no corrupted bit
// pattern to notice. Without this invariant the only way to identify one
// is to compare against neighbouring pixels after the fact, which firmware
// cannot do while streaming and which cannot distinguish a genuine sharp
// feature from a fault.
//
// Recovery is possible because the ADS7279 holds the conversion result on
// DOUT until the next conversion begins, roughly 20 us away. Re-framing
// the transfer with a fresh CS cycle re-reads the SAME conversion, so a
// successful retry returns the true value rather than an interpolation or
// a substitute. This corrects the pixel rather than merely flagging it.
//
// Only one retry is attempted. If it also fails the sample is returned as
// is and counted, because a second failure suggests something other than a
// transient timing race, and spending more of the per-pixel budget chasing
// it risks missing the next pixel outright.
uint32_t g_readRetriesThisFrame = 0;
uint32_t g_readStillBadThisFrame = 0;

uint16_t readAdcSample() {
  // Read the SAME conversion twice and compare.
  //
  // This works because the corruption is probabilistic rather than
  // systematic. Measured shift rate is about 0.29 percent per read, so two
  // independent reads corrupting IDENTICALLY is around 0.0008 percent -
  // three orders of magnitude better - and any disagreement proves at least
  // one of them is wrong without needing to know which.
  //
  // It is only affordable because the transfer was brought down to ~6 us by
  // moving this path into RAM (__not_in_flash_func). From flash it measured
  // ~10 us and two reads would not have fit the ~20 us per-pixel budget.
  //
  // The ADS7279 holds the result on DOUT until the next conversion begins,
  // so the second read returns the same conversion, not a new sample. That
  // is what makes this a genuine repeat measurement rather than two
  // different pixels.
  //
  // On disagreement a third read breaks the tie by median. Median of three
  // is correct whichever direction the corruption went, unlike taking the
  // minimum, which would only handle the left shifts that dominate today
  // and would silently do the wrong thing if right shifts ever became
  // common.
  const uint16_t a = readAdcSampleOnce();
  const uint16_t b = readAdcSampleOnce();

  if (a == b) {
    return a; // agreement: overwhelmingly the common case
  }

  ++g_readRetriesThisFrame;
  const uint16_t c = readAdcSampleOnce();

  // Median of the three.
  uint16_t med;
  if ((a <= b && b <= c) || (c <= b && b <= a)) {
    med = b;
  } else if ((b <= a && a <= c) || (c <= a && a <= b)) {
    med = a;
  } else {
    med = c;
  }

  // No two of the three agreed: the tie-break is a best effort rather than
  // a confirmed value, so count it separately.
  if (a != c && b != c && a != b) {
    ++g_readStillBadThisFrame;
  }

  g_lastReadLowBits = (uint8_t)(med & 0x3);
  return med;
}

// Full-duplex 16-bit transfer, same on-demand bit-banged SCLK as
// readAdcSample(), but also drives SDI (MOSI) with txWord, MSB first.
// Per datasheet: each SDI bit must be stable before the falling edge
// that latches it (tS1/tH1), symmetric with how each SDO bit becomes
// valid at a falling edge and is read on the next rising edge. Only
// used for the boot-time verification below, never in the per-pixel hot
// path.
uint16_t transferAdc16(uint16_t txWord) {
  uint16_t rxWord = 0;

  const uint32_t irqState = save_and_disable_interrupts();

  gpio_put(PIN_ADC_SCLK, false);
  gpio_put(PIN_ADC_MOSI, (txWord & 0x8000) != 0); // MSB ready before CS falls
  gpio_put(PIN_ADC_CS, false);
  // Same CS setup requirement as readAdcSample(); this path has the
  // identical race, and is a candidate explanation for the unreliable CFR
  // readback (Section 9.1) since a shifted result reads as a wrong
  // register value rather than an obviously corrupt one.
  adcCsSetupDelay();

  for (int bit = 0; bit < 16; ++bit) {
    gpio_put(PIN_ADC_SCLK, true); // rising edge: SDO bit is stable
    adcClockDelay();
    rxWord = (rxWord << 1) | (gpio_get(PIN_ADC_MISO) ? 1u : 0u);
    gpio_put(PIN_ADC_SCLK, false); // falling edge: ADC latches SDI, shifts next SDO bit
    adcClockDelay();
    const int nextBit = bit + 1;
    if (nextBit < 16) {
      gpio_put(PIN_ADC_MOSI, (txWord & (0x8000 >> nextBit)) != 0);
    }
  }

  gpio_put(PIN_ADC_CS, true);
  gpio_put(PIN_ADC_MOSI, true); // back to default-mode idle (SDI = +VBD)

  restore_interrupts(irqState);

  return rxWord;
}

// Read back the CFR (command 1100b) to confirm the chip is answering.
// The low 12 bits are the CFR contents; the top 4 are stale
// conversion-result MSBs and can be ignored. Expected on the ADS7279:
// 0xFFD (all 1s except D1, which this part always reads back as 0).
uint16_t readAdcCfr() {
  return transferAdc16(0xC000) & 0x0FFF;
}

void printCfr(const char *label) {
  char line[80];
  snprintf(line, sizeof(line), "BOOT: %s CFR = 0x%03X (expect 0xFFD)", label,
           (unsigned)readAdcCfr());
  Serial.println(line);
}

// Formats a 16-bit value as "0x____ = 16 binary digits" so it can be
// lined up bit-for-bit against a scope capture of SDI.
void printAdcCommand(const char *label, uint16_t word) {
  char bits[17];
  for (int i = 0; i < 16; ++i) {
    bits[i] = (word & (0x8000 >> i)) ? '1' : '0';
  }
  bits[16] = '\0';

  char line[64];
  snprintf(line, sizeof(line), "BOOT: sending %-16s 0x%04X = %s", label,
           (unsigned)word, bits);
  Serial.println(line);
}

// Force the CFR to a known state and read it back after EACH write, so a
// divergence shows up as exactly one bad line instead of only the
// combined end state.
void resetAdcConfig() {
  printCfr("before any write,");

  // Command 1110b (write CFR) + 12 data bits D11..D0, with D0=0: system
  // reset. D0 does NOT self-clear - holding it low leaves the chip
  // permanently in reset (which is exactly what produced the "every
  // pixel = 0x3FFF" symptom: the datasheet's documented reset artifact,
  // seen forever instead of just once). A second write with D0=1 is
  // required to bring the chip back to normal operation afterward.
  printAdcCommand("reset write", 0xEFFE); // reset: D0 = 0
  transferAdc16(0xEFFE);
  printCfr("after reset write,");

  delayMicroseconds(50); // margin in case the reset needs settling time
  printAdcCommand("normal-op write", 0xEFFF); // normal operation: D0 = 1, rest at defaults
  transferAdc16(0xEFFF);
  printCfr("after normal-op write,");
}

// ~5x the expected ~20.5 ms scan (1024 px * ~20 us/px) - the CPLD has gone
// quiet (line stuck/disconnected), not just running a bit slow. Only
// matters now that capture is a blocking loop (see runScan()) instead of
// an ISR that could never itself hang the main loop.
constexpr uint32_t SCAN_WATCHDOG_US = 100000;

// Separate, much tighter bound on the FIRST trigger of a scan.
//
// The whole-scan watchdog above is the wrong instrument for detecting a
// scan that never began. Measured on hardware: the CPLD occasionally does
// not act on a START pulse at all. The signature is unambiguous - pulses 0,
// raw 0 (the UNFILTERED state machine saw zero transitions, so nothing was
// emitted, as opposed to something being emitted and filtered), fifo 0,
// dur equal to SCAN_WATCHDOG_US exactly. Observed once in 322 frames.
//
// Handling that with the 100 ms watchdog is doubly bad: the frame is lost,
// AND the next frame runs ~34 ms late because the capture loop sat blocked
// for 100 ms waiting for a scan that was never coming. The first pixel of a
// healthy scan arrives within tens of microseconds of START, so absence of
// any trigger after this bound is conclusive, not merely suspicious. It is
// set generously above the observed arrival time because the only cost of
// waiting slightly too long is a slightly later retry.
constexpr uint32_t FIRST_TRIGGER_TIMEOUT_US = 2000;

// How many additional START pulses to issue when a scan never begins.
// Re-issuing is safe: the CPLD demonstrably did nothing with the previous
// pulse, so it is idle, and runFrame() clears both FIFOs before each
// attempt. Bounded so a genuinely disconnected CPLD degrades to one short
// frame per period rather than spinning here.
constexpr uint32_t START_RETRY_LIMIT = 3;

// --- PIO-based pixel trigger debounce (see pixel_trig.pio) ---
// Two software approaches were both measured to lose real pixels on
// hardware before this: an ISR-based FALLING-edge attachInterrupt() can
// COALESCE two edges that land closer together than the CPU's interrupt-
// service latency into a single interrupt entry (the RP2350's GPIO
// edge-detect is a single sticky bit per pin per edge type, not a
// counter - a pixel can vanish with zero trace in any software counter,
// because there's nothing to reject; the ISR just never runs a second
// time). A plain CPU busy-poll of the raw pin level was tried next and
// measured WORSE: it can miss a narrow pulse ENTIRELY if any other
// interrupt (USB, systick, ...) preempts the poll loop for the pulse's
// duration - unlike an edge latch, a level read has no memory of what
// happened while the CPU wasn't looking, and there is a lot of other
// interrupt activity on this chip once USB is in the picture.
//
// pixel_trig.pio runs the ARMED/REFRACTORY debounce this file has always
// used, but in PIO hardware: independent of the ARM core's interrupt
// controller entirely, so it can't be preempted, and it reports confirmed
// triggers through a real FIFO (joined to depth 8 below) instead of a
// single bit, so even a brief CPU stall of several pixel periods is
// absorbed rather than silently lost. See pixel_trig.pio's header comment
// for the full reasoning and the exact hardware policy it implements.
PIO g_pixelTrigPio = pio0;
int g_pixelTrigSm = -1;

// REFRACTORY_US expressed in PIO cycles at the actual system clock. The
// SM runs at full clock (divider 1.0) so its pin sampling is fine-grained
// (see initPixelTrigPio()), which means its refractory countdown is in
// cycles, not microseconds. Computed at runtime from clock_get_hz() so a
// change of system clock cannot silently change the refractory duration.
uint32_t refractoryCycles() {
  const uint32_t cyclesPerUs = clock_get_hz(clk_sys) / 1000000u;
  return (REFRACTORY_US * cyclesPerUs) - 1u; // -1: jmp x-- is decrement-then-test
}

void initPixelTrigPio() {
  g_pixelTrigSm = pio_claim_unused_sm(g_pixelTrigPio, true);
  const uint offset = pio_add_program(g_pixelTrigPio, &pixel_trig_program);

  pio_sm_config c = pixel_trig_program_get_default_config(offset);
  sm_config_set_in_pins(&c, PIN_PIXEL_TRIG); // also the base for this program's "wait ... pin" instructions
  sm_config_set_jmp_pin(&c, PIN_PIXEL_TRIG); // used by the bounded re-arm wait's "jmp pin" checks (see REVISION 2 in pixel_trig.pio)
  // FULL system clock, divider 1.0. A PIO "wait" samples its pin once per
  // SM cycle, so the divider IS the edge-detection sampling rate. Earlier
  // revisions divided this down to 1 cycle == 1 us purely so REFRACTORY_US
  // could be pushed in below as a cycle count without conversion - which
  // silently throttled pin sampling to 1 MHz and let ~INT pulses narrower
  // than a microsecond slip between samples entirely, unseen and
  // uncounted. That is the same class of blind-window defect that CPU
  // polling was rejected for. See REVISION 3 in pixel_trig.pio.
  sm_config_set_clkdiv(&c, 1.0f);
  sm_config_set_in_shift(&c, false, false, 32); // shift direction is irrelevant (1 bit/entry); autopush off, this program pushes explicitly
  // Join the RX FIFO with the (unused) TX FIFO for depth 8 instead of 4 -
  // more slack to absorb a CPU stall of several pixel periods before a
  // confirmed trigger would ever have to be dropped.
  sm_config_set_fifo_join(&c, PIO_FIFO_JOIN_RX);

  pio_sm_init(g_pixelTrigPio, g_pixelTrigSm, offset, &c);
  // Seed X before enabling: pixel_trig.pio's first "pull noblock" needs
  // something already in the TX FIFO to load, matching mainboard's
  // adc_convst REVISION 9 "pull noblock" idiom. Now that the SM runs at
  // full clock, the refractory window is counted in CYCLES, so convert
  // here from the wall-clock REFRACTORY_US using the real system clock.
  // REFRACTORY_US stays the single source of truth in microseconds.
  pio_sm_put(g_pixelTrigPio, g_pixelTrigSm, refractoryCycles());
  pio_sm_set_enabled(g_pixelTrigPio, g_pixelTrigSm, true);
}

// Proves, at runtime, WHICH pixel_trig.pio build is actually executing.
//
// pixel_trig.pio.h is a generated file, checked in, with no automated
// regeneration step in this PlatformIO Arduino build (unlike a raw
// Pico-SDK CMake build, which runs pioasm through
// pico_generate_pio_header()). Editing pixel_trig.pio and forgetting to
// re-run pioasm produces a firmware that compiles cleanly, flashes
// cleanly, and silently contains the OLD program. Nothing in the build
// output indicates this. The sibling mainboard project lost real
// debugging time to exactly this failure, chasing timing theories against
// a binary that never contained the edits being tested.
//
// This prints the instruction count and a checksum computed over the
// actual instruction words linked into this binary, plus the divider and
// refractory count actually programmed into the SM. Compare the checksum
// across builds: if pixel_trig.pio changed but this value did not, the
// header is stale and the running firmware does not contain the edit.
void printPioBuildStamp() {
  uint32_t sum = 0;
  for (uint8_t i = 0; i < pixel_trig_program.length; ++i) {
    sum = (sum * 31u) + pixel_trig_program_instructions[i];
  }

  char line[160];
  snprintf(line, sizeof(line),
           "BOOT: pixel_trig.pio len=%u checksum=0x%08lX clkdiv=1.0 "
           "refractory=%luus (%lu cycles @ %luMHz)",
           (unsigned)pixel_trig_program.length, (unsigned long)sum,
           (unsigned long)REFRACTORY_US, (unsigned long)refractoryCycles(),
           (unsigned long)(clock_get_hz(clk_sys) / 1000000u));
  Serial.println(line);
}

// Diagnostic only - see raw_edge_count in pixel_trig.pio. Runs on a
// second SM watching PIN_PIXEL_TRIG with zero filtering, so its count can
// be compared against the debounced pixel_trig count to tell whether
// REFRACTORY_US is discarding genuine pixels or whether pulses are lost
// before either state machine ever observes them.
int g_rawEdgeSm = -1;

void initRawEdgeCountPio() {
  g_rawEdgeSm = pio_claim_unused_sm(g_pixelTrigPio, true);
  const uint offset = pio_add_program(g_pixelTrigPio, &raw_edge_count_program);

  pio_sm_config c = raw_edge_count_program_get_default_config(offset);
  sm_config_set_in_pins(&c, PIN_PIXEL_TRIG);
  // Full clock, same as pixel_trig - and for the same reason. A diagnostic
  // that sampled the pin more coarsely than the state machine it audits
  // would report its own blind windows as genuine signal loss, which is
  // worse than having no diagnostic at all.
  sm_config_set_clkdiv(&c, 1.0f);
  sm_config_set_in_shift(&c, false, false, 32);
  sm_config_set_fifo_join(&c, PIO_FIFO_JOIN_RX); // depth 8 - this SM has no debounce to absorb bounce, so it can push much faster than pixel_trig

  pio_sm_init(g_pixelTrigPio, g_rawEdgeSm, offset, &c);
  pio_sm_set_enabled(g_pixelTrigPio, g_rawEdgeSm, true);
}

// Drains the raw-edge SM's FIFO completely and returns how many words
// were in it - call often (every pixel, from runScan()) since this SM has
// no debounce and can accumulate a bounce-inflated backlog quickly.
uint32_t drainRawEdgeCount() {
  uint32_t n = 0;
  while (!pio_sm_is_rx_fifo_empty(g_pixelTrigPio, g_rawEdgeSm)) {
    (void)pio_sm_get(g_pixelTrigPio, g_rawEdgeSm);
    ++n;
  }
  return n;
}

struct ScanResult {
  uint32_t capturedCount = 0; // pixels genuinely received and read this frame
  uint32_t highestIndex = 0;  // highest buffer slot this frame reached; with positional indexing this can exceed capturedCount
  uint32_t missingCount = 0;  // slots left as MISSING_PIXEL sentinel because their trigger never arrived
  uint32_t rawEdgeCount = 0; // total transitions seen by the unfiltered raw_edge_count SM this frame - see drainRawEdgeCount()
  uint32_t maxRawGapUs = 0;
  uint32_t minRawGapUs = UINT32_MAX;
  uint32_t maxReadDurationUs = 0;
  uint32_t earlyDeassertEdges = 0;
  uint32_t maxFifoBacklog = 0; // largest number of already-confirmed triggers seen still queued when we checked - how close to the depth-8 limit (and to actually dropping one) we got
  uint32_t medianGapUs = 0;    // median of this frame's trustworthy gaps; the period estimate for the NEXT frame
  uint32_t startRetries = 0;   // extra START pulses issued because the scan never began
  bool timedOut = false;
  bool neverStarted = false;   // no trigger at all within FIRST_TRIGGER_TIMEOUT_US: the CPLD ignored START
  uint16_t gapHist[GAP_HIST_BUCKETS] = {0};
  uint32_t lowBitsHist[4] = {0}; // distribution of the 2 unused result bits - see g_lastReadLowBits
};

// Drains pixel_trig.pio's RX FIFO and fills outBuf, stopping when the
// buffer is full or SCAN_WATCHDOG_US elapses. Each FIFO word is an
// already-debounced, hardware-confirmed pixel trigger (see the
// g_pixelTrigPio comment above) - there is no bounce classification left
// to do in software; every word popped here is genuine by construction.
//
// POSITIONAL INDEXING: a pixel's slot in outBuf is derived from WHEN its
// trigger arrived, not from how many triggers have been counted so far.
// This matters because the two disagree exactly when a pixel is lost, and
// that disagreement was the mechanism behind the drifting image.
//
// Under the previous append-in-arrival-order scheme
// (outBuf[capturedCount++] = sample), one lost pixel mid-scan shifted
// every subsequent pixel in that frame one slot earlier. The frame stayed
// internally plausible - no gap, no marker, nothing obviously wrong - but
// the whole tail was spatially wrong, by an amount that varied frame to
// frame with where and how often loss happened. Rendered live, that reads
// as an image sliding sideways, which is precisely the reported symptom.
//
// Here, each trigger's index is the previous index advanced by however
// many pixel periods actually elapsed: steps = round(gap / period). A
// normal gap gives steps == 1. A gap of about two periods gives steps ==
// 2, which places the new pixel in its TRUE slot and leaves the skipped
// slot holding MISSING_PIXEL. Later pixels are unaffected. Loss becomes a
// localized, explicitly marked hole instead of a global shift.
//
// The step calculation is relative to the last observed trigger, not to
// the scan start, so period-estimate error cannot accumulate across the
// frame - each decision is local, and an error in one step does not
// propagate into the next.
ScanResult runScan(uint16_t *outBuf) {
  ScanResult r;
  g_readRetriesThisFrame = 0;
  g_readStillBadThisFrame = 0;
  uint32_t lastAcceptUs = 0;
  uint32_t nextIndex = 0; // slot the next accepted trigger belongs in
  const uint32_t periodUs = g_pixelPeriodUs; // snapshot: must not change mid-scan
  const uint32_t halfPeriodUs = periodUs / 2;
  // True only when we observed an empty FIFO since the last pop, i.e. we
  // were genuinely idle-waiting for this trigger rather than catching up.
  // Without this, a trigger that arrived while readAdcSample() held
  // interrupts off is timestamped late, and both the step calculation and
  // the period estimate inherit that error.
  bool wasIdleWaiting = false;

  // Start fully sentinel-filled. Any slot never written by a real trigger
  // is then already marked as missing, with no separate tail-fill pass and
  // no dependence on losses being contiguous.
  for (uint32_t i = 0; i < PIXEL_COUNT; ++i) {
    outBuf[i] = MISSING_PIXEL;
  }

  const uint32_t scanStartUs = micros();

  while (nextIndex < PIXEL_COUNT) {
    const uint32_t elapsedUs = (uint32_t)(micros() - scanStartUs);
    if (elapsedUs > SCAN_WATCHDOG_US) {
      r.timedOut = true;
      break;
    }
    // Bail out early and decisively if the scan never began at all, rather
    // than blocking for the full watchdog on a scan that is not coming.
    // See FIRST_TRIGGER_TIMEOUT_US; runFrame() retries START on this.
    if (r.capturedCount == 0 && elapsedUs > FIRST_TRIGGER_TIMEOUT_US) {
      r.neverStarted = true;
      break;
    }

    // Drain the unfiltered diagnostic SM every pass so it can't build up
    // a backlog large enough to overflow its own FIFO and undercount.
    r.rawEdgeCount += drainRawEdgeCount();

    const uint32_t fifoLevel = pio_sm_get_rx_fifo_level(g_pixelTrigPio, g_pixelTrigSm);
    if (fifoLevel == 0) {
      wasIdleWaiting = true; // caught up; the next arrival will be seen promptly
      continue;              // no confirmed trigger yet - keep spinning
    }
    if (fifoLevel > r.maxFifoBacklog) {
      r.maxFifoBacklog = fifoLevel;
    }
    (void)pio_sm_get(g_pixelTrigPio, g_pixelTrigSm); // pop it - the payload carries no information, only its presence does

    const uint32_t nowUs = micros();
    uint32_t index = nextIndex;

    if (lastAcceptUs != 0) {
      const uint32_t gap = (uint32_t)(nowUs - lastAcceptUs);
      if (gap > r.maxRawGapUs) {
        r.maxRawGapUs = gap;
      }
      if (gap < r.minRawGapUs) {
        r.minRawGapUs = gap;
      }

      // Decide whether this gap is evidence of a LOST pixel, or merely
      // evidence that WE were late to look.
      //
      // nowUs is when the CPU popped this entry, which is not when the
      // trigger arrived. Those diverge whenever the CPU falls behind: the
      // PIO keeps queuing triggers while we are busy, and a gap inflated
      // purely by our own scheduling would be misread as a missing pixel.
      // Inserting a hole that did not happen shifts every later pixel by
      // one, which is the same drift this indexing scheme exists to
      // prevent - a false positive here is as damaging as the original
      // bug.
      //
      // The FIFO makes the distinction reliable. Its entries are strictly
      // consecutive confirmed triggers; a pixel cannot be lost BETWEEN two
      // entries already sitting in it. So a backlog (level > 1) means we
      // are draining, our timestamp is meaningless, and the correct step
      // is exactly 1. On top of that, wasIdleWaiting confirms we actually
      // sat with an empty FIFO before this arrival, rather than popping a
      // trigger that queued up while readAdcSample() held interrupts off -
      // FIFO level alone cannot distinguish those, and the latter carries
      // several microseconds of timestamp error.
      const bool gapIsTrustworthy = (fifoLevel <= 1) && wasIdleWaiting;

      uint32_t steps = 1;
      if (gapIsTrustworthy && gap >= (periodUs + halfPeriodUs)) {
        steps = (gap + halfPeriodUs) / periodUs;
        if (steps > MAX_INDEX_JUMP) {
          steps = MAX_INDEX_JUMP; // one absurd gap must not fling the index across the frame
        }
      }

      // Feed only trustworthy, single-period gaps to the period estimator.
      // Gaps spanning a genuine hole would bias it upward; jittered ones
      // would bias it in whichever direction the CPU happened to be late.
      if (gapIsTrustworthy && steps == 1) {
        const uint32_t bucket = (gap < GAP_HIST_BUCKETS) ? gap : (GAP_HIST_BUCKETS - 1);
        ++r.gapHist[bucket];
      }

      index = (nextIndex - 1) + steps; // nextIndex-1 is the slot just filled
      if (index >= PIXEL_COUNT) {
        break; // this trigger belongs past the end of the frame
      }
      r.missingCount += (steps - 1);
    }

    const uint32_t readStartUs = micros();
    const uint16_t sample = readAdcSample();
    const uint32_t readDurationUs = (uint32_t)(micros() - readStartUs);
    if (readDurationUs > r.maxReadDurationUs) {
      r.maxReadDurationUs = readDurationUs;
    }
    if (g_lastReadSawEarlyDeassert) {
      ++r.earlyDeassertEdges;
    }
    ++r.lowBitsHist[g_lastReadLowBits & 0x3];

    outBuf[index] = (sample >> 2) & ADC_CODE_MASK;
    ++r.capturedCount;
    r.highestIndex = index;
    nextIndex = index + 1;
    lastAcceptUs = nowUs;
    wasIdleWaiting = false; // must observe an empty FIFO again before trusting the next gap

    pumpTx(); // keep draining the previous frame's staged text between pixels
  }

  // Final drain of the diagnostic SM. The loop exits the instant the last
  // pixel lands, before that iteration's drain, so without this the raw
  // count reads one low on most frames - an artifact of when we stopped
  // looking, not a real discrepancy between the two state machines.
  r.rawEdgeCount += drainRawEdgeCount();

  // Any slot after the last one reached is still sentinel from the
  // pre-fill above; count those too so missingCount covers the whole frame.
  if (r.capturedCount > 0) {
    r.missingCount += (PIXEL_COUNT - 1) - r.highestIndex;
  } else {
    r.missingCount = PIXEL_COUNT;
  }

  // Refresh the period estimate for the NEXT frame from the MEDIAN of this
  // frame's trustworthy gaps (see g_pixelPeriodUs for why the median, and
  // what using the minimum did instead). Requires a meaningful number of
  // samples, and is clamped, so a sparse or pathological frame cannot
  // poison indexing for every frame after it.
  uint32_t samples = 0;
  for (uint32_t i = 0; i < GAP_HIST_BUCKETS; ++i) {
    samples += r.gapHist[i];
  }
  if (samples >= GAP_HIST_MIN_SAMPLES) {
    const uint32_t halfway = samples / 2;
    uint32_t running = 0;
    for (uint32_t i = 0; i < GAP_HIST_BUCKETS; ++i) {
      running += r.gapHist[i];
      if (running >= halfway) {
        r.medianGapUs = i;
        break;
      }
    }
    if (r.medianGapUs >= PIXEL_PERIOD_MIN_US && r.medianGapUs <= PIXEL_PERIOD_MAX_US) {
      g_pixelPeriodUs = r.medianGapUs;
    }
  }

  return r;
}

// Fires START, runs the full synchronous scan, and stages this frame's
// text for non-blocking transmission. Renamed from the earlier
// beginFrame(): capture used to happen in the background via ISR while
// this function only did bookkeeping around an already-in-progress
// buffer; now it runs the whole frame start to finish.
void runFrame(uint32_t frameStartTimeUs) {
  // How overdue this call is relative to its own schedule - should stay
  // small and stable; if it doesn't, something is stalling loop() itself
  // (not a blocked Serial write - pumpTx() never blocks - so look
  // elsewhere).
  const uint32_t lateUs = (uint32_t)(frameStartTimeUs - g_nextFrameStartUs);
  const uint32_t entryUs = micros();

  // Issue START and capture, retrying if the CPLD ignores the pulse
  // entirely. Measured on hardware this occurs roughly once in a few
  // hundred frames: the CPLD does not begin a scan and emits nothing at
  // all, which the diagnostics show unambiguously as pulses 0 / raw 0 /
  // fifo 0 (the unfiltered state machine saw no transitions either, so
  // nothing was emitted rather than something being emitted and filtered).
  //
  // Before this loop existed, such a frame cost far more than itself: the
  // capture loop blocked for the full 100 ms watchdog, so the frame was
  // lost AND the following frame ran ~34 ms late. Retrying turns that into
  // roughly 2 ms of delay and a normal frame. Re-issuing START is safe
  // precisely because the CPLD demonstrably did nothing with the previous
  // pulse and is therefore idle.
  //
  // The FIFO clear lives inside this loop rather than before it. It
  // discards anything left from a previously abandoned scan - the CPLD has
  // no way to know we gave up, so it keeps emitting triggers for the rest
  // of that old scan, and pixel_trig.pio keeps queuing them because it has
  // no concept of the software having stopped listening. Popped as the next
  // frame's pixel 1, 2, ..., those would misalign the new frame as well,
  // turning one bad frame into a cascade. Each retry needs the same clean
  // starting state. Both TX FIFOs are cleared too; pixel_trig's is empty in
  // normal operation (see initPixelTrigPio()'s "pull noblock" comment), so
  // this does not disturb its persisted refractory count in X.
  ScanResult r;
  for (uint32_t attempt = 0;; ++attempt) {
    pio_sm_clear_fifos(g_pixelTrigPio, g_pixelTrigSm);
    pio_sm_clear_fifos(g_pixelTrigPio, g_rawEdgeSm);

    digitalWrite(PIN_START, HIGH);
    delayMicroseconds(START_PULSE_WIDTH_US);
    digitalWrite(PIN_START, LOW);

    r = runScan(g_pixelBuf);
    r.startRetries = attempt;

    if (!r.neverStarted || attempt >= START_RETRY_LIMIT) {
      break;
    }
  }

  g_nextFrameStartUs = frameStartTimeUs + g_framePeriodUs;

  const uint32_t doneFrameSeq = g_frameSequence;
  ++g_frameSequence;

  // No tail-fill pass here any more. runScan() pre-fills the whole buffer
  // with MISSING_PIXEL before capturing, so every slot a real trigger did
  // not write is already marked - wherever it is, not just in a trailing
  // run. That also removes the old zero-fill's ambiguity, since 0 is a
  // perfectly legal reading and MISSING_PIXEL is not.

  // Pixel ordinals are 1-based; the buffer is 0-indexed.
  const uint16_t code = (REPORT_PIXEL_INDEX <= PIXEL_COUNT)
                             ? g_pixelBuf[REPORT_PIXEL_INDEX - 1]
                             : MISSING_PIXEL;
  const uint32_t millivolts = (code == MISSING_PIXEL)
                                  ? 0
                                  : ((uint32_t)code * ADC_VREF_MV) / ADC_FULL_SCALE;

  // durationUs now spans the ENTIRE synchronous scan (START through the
  // last captured/rejected pixel) - normally ~20 ms (1024 px * ~20 us),
  // not a fault indicator by itself any more. "late" is still the number
  // to watch for scheduling drift.
  const uint32_t durationUs = (uint32_t)(micros() - entryUs);

  // Stage this frame's text for non-blocking transmission - but only if
  // the PREVIOUS frame's text has fully drained. If the host is falling
  // behind USB bandwidth-wise rather than just momentarily busy, skip
  // this frame's text outright instead of overwriting a buffer still
  // being sent from: live_view.py already renders whatever the newest
  // complete frame it received is, so a dropped frame here is a skipped
  // display update, not corrupted data.
  // Frame and status are appended as one message so they cannot be split
  // by a reply arriving between them.
  static char frameBuf[FRAME_BINARY_BYTES + 384];
  if (txFree() >= sizeof(frameBuf)) {
    size_t off = stageBinaryFrame(frameBuf, doneFrameSeq,
                                  r.capturedCount, g_pixelBuf);
    off += snprintf(frameBuf + off, sizeof(frameBuf) - off,
             "S %-6lu pulses %4lu raw %4lu miss %4lu period %2luus wdog %1d "
             "nostart %1d retry %1lu early %3lu fifo %1lu gap %3lu-%4luus "
             "late %4luus dur %6luus maxread %2luus txdrop %4lu "
             "lowbits %4lu/%4lu/%4lu/%4lu reread %3lu bad %3lu "
             "px%lu %5u (0x%04X, %4lu mV)\n",
             (unsigned long)doneFrameSeq, (unsigned long)r.capturedCount,
             (unsigned long)r.rawEdgeCount, (unsigned long)r.missingCount,
             (unsigned long)g_pixelPeriodUs,
             (int)r.timedOut, (int)r.neverStarted,
             (unsigned long)r.startRetries, (unsigned long)r.earlyDeassertEdges,
             (unsigned long)r.maxFifoBacklog,
             (unsigned long)(r.minRawGapUs == UINT32_MAX ? 0 : r.minRawGapUs),
             (unsigned long)r.maxRawGapUs, (unsigned long)lateUs,
             (unsigned long)durationUs, (unsigned long)r.maxReadDurationUs,
             (unsigned long)g_txDroppedFrames,
             (unsigned long)r.lowBitsHist[0], (unsigned long)r.lowBitsHist[1],
             (unsigned long)r.lowBitsHist[2], (unsigned long)r.lowBitsHist[3],
             (unsigned long)g_readRetriesThisFrame,
             (unsigned long)g_readStillBadThisFrame,
             (unsigned long)REPORT_PIXEL_INDEX,
             (unsigned)code, (unsigned)code, (unsigned long)millivolts);
    txAppend(frameBuf, off);
  } else {
    ++g_txDroppedFrames;
  }
  pumpTx(); // kick off draining now; loop() and the next scan's poll continue it, non-blocking
}

// --- Host command interface -------------------------------------------
//
// Plain ASCII request/response on the same port the frame stream uses. The
// host application depends on this to identify the device and to read back
// acquisition parameters rather than assuming them, so the reply format is
// a contract: keep it machine-parseable and additive. Adding new key=value
// fields is safe; renaming or reordering existing ones is not.
constexpr char FW_VERSION[] = "1.1.0";
constexpr char FW_BOARD[] = "rpipico2";

// Emitted on request and once at boot. One line, leading "I ", space
// separated key=value pairs, no spaces inside a pair.
void emitDeviceInfo() {
  uint32_t pioSum = 0;
  for (uint8_t i = 0; i < pixel_trig_program.length; ++i) {
    pioSum = (pioSum * 31u) + pixel_trig_program_instructions[i];
  }

  // __DATE__ is "Mmm dd yyyy" with embedded spaces, which would split into
  // three tokens and truncate the value at the first space. Values in this
  // line must not contain spaces; substitute underscores.
  char built[16];
  snprintf(built, sizeof(built), "%s", __DATE__);
  for (char *p = built; *p; ++p) {
    if (*p == ' ') *p = '_';
  }

  char line[320];
  snprintf(line, sizeof(line),
           "I fw=%s proto=2 board=%s pixels=%lu bits=14 vref_mv=%lu "
           "rate_hz=%lu rate_min=%lu rate_max=%lu pixel_period_us=%lu "
           "frame_us=%lu frame_us_min=%lu frame_us_max=%lu "
           "refractory_us=%lu sysclk_mhz=%lu pio_crc=0x%08lX "
           "missing_code=0x%04X built=%s",
           FW_VERSION, FW_BOARD, (unsigned long)PIXEL_COUNT,
           (unsigned long)ADC_VREF_MV, (unsigned long)g_frameRateHz,
           (unsigned long)FRAME_RATE_MIN_HZ, (unsigned long)FRAME_RATE_MAX_HZ,
           (unsigned long)g_pixelPeriodUs,
           (unsigned long)g_framePeriodUs,
           (unsigned long)FRAME_PERIOD_MIN_US,
           (unsigned long)FRAME_PERIOD_MAX_US,
           (unsigned long)REFRACTORY_US,
           (unsigned long)(clock_get_hz(clk_sys) / 1000000u),
           (unsigned long)pioSum, (unsigned)MISSING_PIXEL, built);
  txLine(line);
}

void handleCommand(const char *cmd) {
  if (strcmp(cmd, "id") == 0 || strcmp(cmd, "ver") == 0) {
    emitDeviceInfo();
    return;
  }

  if (strncmp(cmd, "rate ", 5) == 0) {
    long hz = atol(cmd + 5);
    if (hz < 0) hz = 0;
    if (hz > (long)FRAME_RATE_MAX_HZ) hz = FRAME_RATE_MAX_HZ;
    if (hz != 0 && hz < (long)FRAME_RATE_MIN_HZ) hz = FRAME_RATE_MIN_HZ;
    g_frameRateHz = (uint32_t)hz;
    // Zero period means the next frame is always due, so runFrame() is
    // re-entered as soon as the previous one returns.
    if (hz == 0) {
      g_framePeriodUs = 0;
    } else {
      uint32_t us = 1000000UL / g_frameRateHz;
      if (us < FRAME_PERIOD_MIN_US) us = FRAME_PERIOD_MIN_US;
      g_framePeriodUs = us;
    }
    char line[64];
    snprintf(line, sizeof(line), "A rate_hz=%lu", (unsigned long)g_frameRateHz);
    txLine(line);
    return;
  }

  if (strncmp(cmd, "period ", 7) == 0) {
    long us = atol(cmd + 7);
    if (us != 0) {
      if (us < (long)FRAME_PERIOD_MIN_US) us = FRAME_PERIOD_MIN_US;
      if (us > (long)FRAME_PERIOD_MAX_US) us = FRAME_PERIOD_MAX_US;
    }
    g_framePeriodUs = (uint32_t)us;
    // Rate is derived for reporting only; the period is what is applied,
    // so no resolution is lost to the round trip.
    g_frameRateHz = (us > 0) ? (uint32_t)(1000000L / us) : 0;
    char line[80];
    snprintf(line, sizeof(line), "A frame_us=%lu rate_hz=%lu",
             (unsigned long)g_framePeriodUs, (unsigned long)g_frameRateHz);
    txLine(line);
    return;
  }

  char line[96];
  snprintf(line, sizeof(line), "E unknown_command=%.48s", cmd);
  txLine(line);
}

// Non-blocking: consumes whatever has arrived and returns. Called from
// loop() alongside pumpTx(), never from inside a scan, so a command can
// never perturb pixel timing.
void pollHostCommands() {
  static char buf[48];
  static uint8_t len = 0;

  // No gating on transmit state. Replies are appended to the same queue as
  // frames (see TX_CAPACITY), so they cannot interleave, and suppressing
  // command handling while that queue is busy previously made the device
  // unreachable whenever frames ran back to back.

  while (Serial.available() > 0) {
    const char ch = (char)Serial.read();
    if (ch == '\n' || ch == '\r') {
      if (len > 0) {
        buf[len] = '\0';
        handleCommand(buf);
        len = 0;
      }
    } else if (len < sizeof(buf) - 1) {
      buf[len++] = ch;
    } else {
      len = 0; // overlong line: discard rather than truncate into a command
    }
  }
}

void setup() {
  pinMode(PIN_START, OUTPUT);
  digitalWrite(PIN_START, LOW);

  pinMode(PIN_ADC_CS, OUTPUT);
  digitalWrite(PIN_ADC_CS, HIGH);

  pinMode(PIN_ADC_MISO, INPUT);
  pinMode(PIN_ADC_MOSI, OUTPUT);
  // SDI held HIGH = command 1111b ("default mode") on every transaction,
  // per datasheet: "A default mode command can be achieved by simply
  // tying SDI to +VBD." Previously this was LOW, sending the undocumented
  // command 0000b on every read instead - likely why conversions always
  // read back as zero regardless of REF+/+IN/-IN/CONVST all being good.
  digitalWrite(PIN_ADC_MOSI, HIGH);

  // SCLK is bit-banged on demand inside readAdcSample()/transferAdc16(),
  // not free-running - idle low between transfers.
  pinMode(PIN_ADC_SCLK, OUTPUT);
  digitalWrite(PIN_ADC_SCLK, LOW);

  Serial.begin(115200);
  // Block here until a terminal actually opens the port (Serial reads
  // false until DTR is asserted), instead of a fixed delay - so the boot
  // sequence below can never run before the monitor is connected and
  // scroll past unseen. Nothing else happens (no CONVST-driven capture
  // yet) while this waits.
  while (!Serial) {
    delay(10);
  }
  delay(100); // brief settle so the very first line isn't clipped

  Serial.println("BOOT: RP2350 firmware active");
  Serial.println("BOOT: ~INT debounced in PIO hardware (see pixel_trig.pio), not an ISR");
  printPioBuildStamp();
  emitDeviceInfo(); // host app parses this; also available on demand via "id"

  // Reset the ADC before wiring up per-pixel reads, so a bad chip state
  // shows up as one clear readback line instead of another round of
  // ambiguous streamed data.
  resetAdcConfig();

  // No attachInterrupt() here - PIN_PIXEL_TRIG is watched by pixel_trig.pio
  // instead (see the g_pixelTrigPio comment for why). The pull-up still
  // matters: PIO reads the same physical pad, so it needs the line to idle
  // HIGH just as much as software polling did.
  pinMode(PIN_PIXEL_TRIG, INPUT_PULLUP);
  initPixelTrigPio();
  initRawEdgeCountPio(); // diagnostic only - see the g_rawEdgeSm comment

  const uint32_t nowUs = micros();
  g_nextFrameStartUs = nowUs;
  runFrame(nowUs);
}

void loop() {
  const uint32_t nowUs = micros();

  if ((int32_t)(nowUs - g_nextFrameStartUs) >= 0) {
    runFrame(nowUs);
  }

  // Keep draining any staged frame text - non-blocking, see g_txStaging.
  pumpTx();

  // Host commands are handled here, between frames, so they cannot
  // perturb pixel timing inside a scan.
  pollHostCommands();
}
