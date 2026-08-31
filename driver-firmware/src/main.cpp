#include <Arduino.h>
#include <hardware/gpio.h>
#include <hardware/sync.h>
#include <hardware/pio.h>
#include <hardware/clocks.h>
#include <hardware/timer.h>
#include <hardware/structs/sio.h>
#include <pico/platform.h>
#include <math.h>
#include <string.h>
#include <strings.h>
#include <ctype.h>
#include <SPI.h>
#include <SdFat.h>
#include <LittleFS.h>
#include "pixel_trig.pio.h"

// Hardware configuration.

constexpr uint8_t PIN_START = 7;       // OUTPUT: starts one CPLD scan cycle
constexpr uint8_t PIN_PIXEL_TRIG = 1;  // INPUT:  one pulse per pixel, watched by PIO
constexpr uint8_t PIN_ADC_SCLK = 10;   // OUTPUT: SPI clock
constexpr uint8_t PIN_ADC_MISO = 8;    // INPUT:  ADS7279 SDO
constexpr uint8_t PIN_ADC_MOSI = 12;   // OUTPUT: ADS7279 SDI, held high (see setup)
                                       // GPIO 12 is header pin 16. Moved here
                                       // from GPIO 11, which would not drive.
constexpr uint8_t PIN_ADC_CS = 9;      // OUTPUT: ADS7279 chip select

// SD card, on SPI0. The ADC above is bit-banged and uses no SPI
// peripheral, so the card has the port to itself.
constexpr uint8_t PIN_SD_MISO = 16;    // INPUT:  card DAT0
constexpr uint8_t PIN_SD_CS = 17;      // OUTPUT: card chip select
constexpr uint8_t PIN_SD_SCLK = 18;    // OUTPUT: card clock
constexpr uint8_t PIN_SD_MOSI = 19;    // OUTPUT: card CMD

// A transfer word is a 4-bit command in D[15:12] plus 12 bits of CFR data in
// D[11:0]. See ADS7279 datasheet Table 3, "Command Set Defined by Command
// Register (CMR)".
constexpr uint16_t ADC_CMD_READ_CFR = 0xC000;  // command 1100b: read CFR
constexpr uint16_t ADC_CMD_READ_DATA = 0xD000; // command 1101b: read data
constexpr uint16_t ADC_CMD_WRITE_CFR = 0xE000; // command 1110b: write CFR
constexpr uint16_t ADC_CFR_MASK = 0x0FFF;      // the 12 CFR bits, D[11:0]
// Bits the ADS7279 does not implement. Both read back 0 whatever you write.
//
//   D8 - Table 4 marks this bit "Don't care" in both columns.
//   D1 - the TAG bit. The datasheet states that the ADS7279 ignores it and
//        always reads it as 0. Only the ADS7280 implements it.
//
// The datasheet says the CFR default is "all 1s (except for CFR_D1)". That
// sentence names D1 alone, but D8 is unimplemented in the same table, and a
// working part reads back EFD, not FFD. Measured on hardware 2026-08-29.
// Ignore both bits when comparing, or a healthy ADC reports match=0/8.
constexpr uint16_t ADC_CFR_IGNORED = 0x0102;  // D8 and D1

// The value a working ADS7279 returns from a read of an unwritten CFR.
constexpr uint16_t ADC_CFR_DEFAULT = 0x0FFF & ~ADC_CFR_IGNORED;  // 0xEFD

// Write values. Writing 1 to an ignored bit is harmless, so these stay all
// ones. Only D0 differs between them.
constexpr uint16_t ADC_CFR_RESET = 0xFFE;    // D0 low: assert software reset
constexpr uint16_t ADC_CFR_NORMAL = 0xFFF;   // D0 high: normal operation

// What SDO returns in the one cycle immediately after a reset.
//
// The datasheet says SDO "is set to all 1s" there. That describes the 14-bit
// result register, not the whole 16-bit transfer. SDO always sends the 14
// result bits followed by a fixed '00', so a saturated result reads FFFC, not
// FFFF. Measured on hardware 2026-08-29: FFFC.
constexpr uint16_t ADC_POST_RESET_WORD = 0xFFFC;  // 14 ones, then the '00' pad

constexpr uint16_t ADC_CODE_MASK = 0x3FFF;  // 14-bit result, left-justified on the wire
constexpr uint32_t ADC_VREF_MV = 2500;      // REF5025 reference voltage
constexpr uint32_t ADC_FULL_SCALE = 1u << 14; // 16384 codes across VREF

constexpr uint16_t MISSING_PIXEL = 0xFFFF; // the value assigned to missing pixels

static inline uint16_t adcCodeFromWord(uint16_t word) {
  return (uint16_t)((word >> 2) & ADC_CODE_MASK);
}

// Acquisition parameters.

constexpr uint32_t PIXEL_COUNT = 1024;  // number of expected pixels per scan
constexpr uint32_t START_PULSE_WIDTH_US = 5;

// The PIO program converts this time window to system-clock cycles.
constexpr uint32_t REFRACTORY_US = 5; // debouncing time for the ~INT pin

constexpr uint32_t PIXEL_PERIOD_NOMINAL_US = 20; // typical time between pixel reads
constexpr uint32_t PIXEL_PERIOD_MIN_US = 4;
constexpr uint32_t PIXEL_PERIOD_MAX_US = 200;

constexpr uint32_t MAX_INDEX_JUMP = 8;

constexpr uint32_t GAP_HIST_BUCKETS = 64;
constexpr uint32_t GAP_HIST_MIN_SAMPLES = 64;

// Upper bound on how long runScan() will wait for a frame's worth of
// triggers. The loop's normal exit is the index reaching PIXEL_COUNT. If
// pixels are lost it never gets there, and without this it would spin
// forever. Derived per scan from the period in use rather than fixed,
// because the "period" command can set anything up to PIXEL_PERIOD_MAX_US.
//
// A fixed 100000 was used before. That is nearly five times the real 20.5 ms
// scan, so a damaged frame burned 100 ms, overran the 66.7 ms frame period,
// and dragged the following frames late. Measured: wdog 1, dur 100146us,
// then late 34017us on the next frame.
static inline uint32_t scanWatchdogUs(uint32_t periodUs) {
  const uint32_t expectedUs = PIXEL_COUNT * periodUs;
  return expectedUs + (expectedUs / 2);  // 50 percent margin
}

constexpr uint32_t FIRST_TRIGGER_TIMEOUT_US = 2000;
constexpr uint32_t START_RETRY_LIMIT = 3;

constexpr uint32_t FRAME_RATE_HZ = 15;
constexpr uint32_t FRAME_PERIOD_US = 1000000UL / FRAME_RATE_HZ;
constexpr uint32_t FRAME_PERIOD_MIN_US = 20600;
constexpr uint32_t FRAME_PERIOD_MAX_US = 10000000;  // 10 s, for weak signals
constexpr uint32_t FRAME_RATE_MIN_HZ = 1;
constexpr uint32_t FRAME_RATE_MAX_HZ = 60;

constexpr uint32_t REPORT_PIXEL_INDEX = 500;

// How often the capture loop drains the serial queue, in pixels.
//
// 1, i.e. every pixel. Do not raise this. Measured 2026-08-28:
//
//   every pixel     stale on ~36 percent of frames, ~0.38 per frame
//   every 16 pixels stale on ~60 percent of frames, ~0.66 per frame
//
// The reasoning that batching would help was wrong. pumpTx() calls
// Serial.write(buf, chunk) where chunk is whatever has accumulated, so its
// cost is dominated by bytes moved, not by call overhead. The frame's ~2100
// bytes have to go out either way. Batching does not remove that work, it
// concentrates it: fifteen passes do nothing and the sixteenth moves sixteen
// pixels' worth, which is long enough to overrun the 20 us pixel period.
//
// Spread thin, each chunk fits in the slack after the 6 us read delay and the
// 3.5 us transfer. That is what this loop wants.
constexpr uint32_t TX_PUMP_INTERVAL_PIXELS = 1;

// LIVE streams every frame, which is what this firmware has always done.
// QUIET still runs the scan but stages nothing. CAPTURE feeds each frame into
// the stack instead of transmitting it.
//
// QUIET keeps scanning on purpose. The sensor integrates whether or not
// anything reads it, so a device that stops pulsing START returns a saturated
// frame when it starts again. Scanning without transmitting costs nothing and
// keeps the detector in a known state, which also means the first frame after
// a capture is usable immediately.
//
// Declared here rather than beside the stacking code because everything that
// writes to the transmit queue has to be able to see it, and some of that is
// far above the stacking section.
enum RunMode : uint8_t {
  MODE_LIVE = 0,
  MODE_QUIET = 1,
  MODE_CAPTURE = 2,
};

RunMode g_mode = MODE_LIVE;

// Capture state.

struct FrameState {
  uint16_t pixelBuffer[PIXEL_COUNT];
  uint32_t pixelPeriodUs = PIXEL_PERIOD_NOMINAL_US;
  uint32_t frameRateHz = FRAME_RATE_HZ;
  uint32_t framePeriodUs = FRAME_PERIOD_US;
  uint32_t nextFrameStartUs = 0;
  uint32_t sequence = 0;
};

struct AdcDiagnostics {
  bool triggerHighAtReadEnd = false;
  uint8_t lastReadLowBits = 0;
  uint16_t lastRawWord = 0;
};

// How much text goes out with each frame.
//
//   0  no S line at all. Binary frame only.
//   1  the documented S line: every field the host parses.
//   2  everything, including the fault-hunting fields.
//
// The S line is about 330 bytes at level 2 and 185 at level 1, against 2062
// bytes of binary frame, so this is 14 percent of the traffic at most. Level
// 0 exists to take the text out of the picture entirely when testing whether
// our own USB transmission is disturbing the capture.
constexpr uint32_t TX_VERBOSITY_DEFAULT = 1;

struct DiagnosticSettings {
  uint32_t sclkDelayNops = 0;
  uint32_t txVerbosity = TX_VERBOSITY_DEFAULT;
  uint32_t readDelayUs = 6;
};

struct FilteredTriggerCount {
  uint32_t frameSequence = 0;
  uint32_t eventCount = 0;
  bool active = false;
  uint32_t lastEventUs = 0;
  uint32_t firstExtraGapUs = 0;
  uint32_t minExtraGapUs = UINT32_MAX;
  uint32_t maxExtraGapUs = 0;
};

FrameState frameState;
AdcDiagnostics adcDiagnostics;
DiagnosticSettings diagnosticSettings;
FilteredTriggerCount filteredTriggerCount;

// Temporary aliases keep the capture code readable while state ownership is
// consolidated. Remove them when the remaining functions use the state objects.
uint16_t (&pixelBuffer)[PIXEL_COUNT] = frameState.pixelBuffer;
uint32_t &g_pixelPeriodUs = frameState.pixelPeriodUs;
uint32_t &g_frameRateHz = frameState.frameRateHz;
uint32_t &g_framePeriodUs = frameState.framePeriodUs;
uint32_t &g_nextFrameStartUs = frameState.nextFrameStartUs;
uint32_t &frameSequence = frameState.sequence;
bool &g_triggerHighAtReadEnd = adcDiagnostics.triggerHighAtReadEnd;
uint8_t &g_lastReadLowBits = adcDiagnostics.lastReadLowBits;
uint16_t &g_lastRawWord = adcDiagnostics.lastRawWord;
uint32_t &g_sclkDelayNops = diagnosticSettings.sclkDelayNops;
uint32_t &g_readDelayUs = diagnosticSettings.readDelayUs;
uint32_t &g_txVerbosity = diagnosticSettings.txVerbosity;

// All serial output uses this non-blocking queue.


// Queue for data to be held in between when we recieve it from the sensor 
//  and when we send it to the comptuer
// basically the queue is a long array. when we get data from the ADC, we add it to this array.
// the head points to where data is added next, ie the top of the queue.
// as we collect data, we want to send data to the computer in parallel. this is done by processing the tail of the queue. 
// the space between the head and tail represents the data that still needs to be sent. data before the tail has already been sent.

constexpr size_t TX_CAPACITY = 2 * PIXEL_COUNT + 2048; // each pixel is 14 bits, approx 2 bytes. add on 2048 bytes extra for other information to send
struct TxQueue {
  uint8_t buffer[TX_CAPACITY];
  size_t head = 0;
  size_t tail = 0;
  uint32_t droppedFrames = 0;
};

TxQueue txQueue;

// Set while a stack result record is being fed into the queue.
//
// A frame is staged with one txAppend and therefore cannot be divided. A
// result record cannot: it is three times the size of the queue and goes in a
// chunk at a time over many loop() passes. Anything else appended in between
// lands INSIDE the record, and the host then reads a record whose length is
// wrong, fails its checksum, resynchronises by a byte and never completes it.
//
// That is not hypothetical. reportFilteredTriggerCount() writes about 110
// bytes on every frame, so the first transfer to run for longer than one
// frame period was cut apart by it and the capture hung at "collecting
// result".
//
// txLine() refuses while this is set. Text is dropped rather than deferred,
// because a queue of deferred lines is a second thing that can go wrong and
// the transfer lasts well under a second.
bool g_txBinaryLock = false;

// Free space in the USB CDC TX FIFO when nothing is queued. Sampled once at
// boot, then used to tell whether the host has collected what we sent.
int g_txFifoCapacity = 0;

uint8_t (&g_txBuf)[TX_CAPACITY] = txQueue.buffer;
size_t &g_txHead = txQueue.head;
size_t &g_txTail = txQueue.tail;
uint32_t &g_txDroppedFrames = txQueue.droppedFrames;

size_t txPending() { return g_txHead - g_txTail; } // number of bytes that are awaiting transmission
size_t txFree() { return TX_CAPACITY - txPending(); } // how much space is left in the queue. only data that is awaiting transmission needs to be saved.

// function for appending to the queue
// *data is a pointer to the first byte to be added to the queue and len is the number of bytes to add.
bool txAppend(const void *data, size_t len) {
  if (len > txFree()) { // ensure that we dont overfill the queue capacity when adding our new data of length len
    return false;
  }
  if (g_txHead + len > TX_CAPACITY) { // if the write position reaches the end of the array
    memmove(g_txBuf, g_txBuf + g_txTail, txPending()); // move all of the pending bytes back to the front via pointer arithmetic
    g_txHead = txPending(); // update the head position according
    g_txTail = 0; // update the tail position
  }
  // now that we have ensured we have room to store all the data
  memcpy(g_txBuf + g_txHead, data, len);  // copy all that data to the queue.
  g_txHead += len; // update the head
  return true;
}

// send the data in queue via serial out
void pumpTx() {
  if (txPending() == 0) { // if we have no more data pending to be sent
    g_txHead = g_txTail = 0; // move the head and tail back to origin
    return;
  }
  const int avail = Serial.availableForWrite(); // returns how many bytes the serial port can accept
  if (avail <= 0) { // not able to send anything
    return;
  }

  // check how large the packet is compared to the current port availability
  size_t chunk = txPending();
  if (chunk > (size_t)avail) { // if the packet doesnt fit, send as much as we can right now
    chunk = (size_t)avail;
  }
  Serial.write(g_txBuf + g_txTail, chunk); // write the chunk to serial, update the tail
  g_txTail += chunk;
  if (txPending() == 0) { // see if we cleared out the queue yet.
    g_txHead = g_txTail = 0;
  }
}

// accepts a C string and loads it into the queue.
// 510 character max limit. +1 newline.
//
// The buffer was 320 bytes. emitDeviceInfo() outgrew that when the stacking
// fields were added, and the truncation here is silent: the line simply
// arrives short and the host parses whatever survived. Sized with headroom
// rather than to fit, because that failure leaves no trace.
bool txLine(const char *text) {
  if (g_txBinaryLock) {
    return false;  // see g_txBinaryLock: text here would split the record
  }
  const size_t n = strlen(text); // # of characters in text, each character is 1 byte
  if (n + 1 > txFree()) { // if # of characters + /n char exceeds the number of free bytes left in queue. 
    return false; // doesnt fit so we dont do anything
  }
  char buf[512]; // define character buffer that can hold 512 bytes max
  // silently truncates the text to fit in the buffer
  const size_t take = (n < sizeof(buf) - 2) ? n : sizeof(buf) - 2; // if n < 318 chars, take=n. else, take=318
  memcpy(buf, text, take); // copy that text packet into the buffer
  buf[take] = '\n'; // append a newline to the end
  return txAppend(buf, take + 1); // TODO can probably remove the +1 here?
}

// Whether a live frame of `need` bytes may enter the queue right now.
//
// The frame path is the other writer that is not part of a record, and it
// has to refuse for the same reason txLine() refuses: a record is fed in a
// chunk at a time over many loop() passes, and anything appended in between
// lands INSIDE it. The host then reads a record whose length is wrong, fails
// its checksum, resynchronises by a byte and never completes it.
//
// The test lives here rather than as a condition inside runFrame() so that
// there is one place both non-record writers ask, and so test/harness can
// reach it. It was a bare txFree() check inside runFrame() before, which is
// why reading a file off the card failed every time: a capture leaves the
// device QUIET and stages no frames, so a result transfer never met this,
// but the live view keeps running while somebody browses the card.
//
// The caller drops the frame. It does not wait for one. The host shows the
// newest complete frame, so a dropped frame is a missed update rather than
// corrupt data, and a transfer lasts well under a second.
bool txFrameReady(size_t need) {
  if (g_txBinaryLock) {
    return false;
  }
  return txFree() >= need;
}

// Protocol 2 frame encoding: little-endian fields followed by a data checksum.

constexpr uint8_t FRAME_MAGIC[4] = {0xA5, 0x5A, 0xD5, 0x1C}; // start sequence ID of a frame
constexpr size_t FRAME_BINARY_BYTES =
    sizeof(FRAME_MAGIC) + 4 + 2 + 2 + (2 * PIXEL_COUNT) + 2; // size of a frame
    // 4 bytes for the start ID. 4 bytes for the sequence number. 
    // 2 bytes for captured pixel count. 
    // 2 bytes for total pixel count. 
    // 2*pixel count for the pixel values. 
    // 2 bytes for a checksum

// Writes one binary frame to buf
// buf: destination buffer
// seq: frame sequence number
// capturedCount: number of pixels captured in this frame
// pixels: array of PIXEL_COUNT pixel values
// Returns the number of bytes written to buf
size_t stageBinaryFrame(char *buf, uint32_t seq, uint32_t capturedCount,
                        const uint16_t *pixels) {
  uint8_t *out = (uint8_t *)buf; //destination pointer. cast from char* to uint8_t*

  memcpy(out, FRAME_MAGIC, sizeof(FRAME_MAGIC)); // copies the frame start sequence to the output pointer
  out += sizeof(FRAME_MAGIC); // increment the pointer

  *out++ = (uint8_t)(seq & 0xFF); // extract the first byte
  *out++ = (uint8_t)((seq >> 8) & 0xFF); // extract second byte
  *out++ = (uint8_t)((seq >> 16) & 0xFF); // extract third byte
  *out++ = (uint8_t)((seq >> 24) & 0xFF); // extract fourth byte

  *out++ = (uint8_t)(capturedCount & 0xFF); // extract first byte
  *out++ = (uint8_t)((capturedCount >> 8) & 0xFF); // extract second byte

  *out++ = (uint8_t)(PIXEL_COUNT & 0xFF); // extract first byte
  *out++ = (uint8_t)((PIXEL_COUNT >> 8) & 0xFF); // extract second byte

  // compute checksum
  uint16_t checksum = 0;
  for (uint32_t i = 0; i < PIXEL_COUNT; ++i) {
    const uint16_t v = pixels[i];
    checksum = (uint16_t)(checksum + v);
    *out++ = (uint8_t)(v & 0xFF); // each pixel is 2 bytes, so we write the first byte
    *out++ = (uint8_t)((v >> 8) & 0xFF); // followed by second byte
  }

  *out++ = (uint8_t)(checksum & 0xFF); // finally we append the 2 checksum bytes
  *out++ = (uint8_t)((checksum >> 8) & 0xFF);

  return (size_t)((char *)out - buf); // return the # of bytes written
}

// ===========================================================================
// ADC SPI, bit-banged
//
// Firmware generates SCLK for each transfer. The clock does not free run. An
// earlier design used a free-running PIO clock. Its phase drifted against the
// CPLD pixel timing and produced a slow artifact in the image. A clock that
// starts with each transfer cannot drift.
//
// These functions run from RAM. __not_in_flash_func() puts them there. From
// flash, one transfer took approximately 10 us. That left no time to read the
// same pixel two times.
// ===========================================================================

// Half period between SCLK edges.
//
// The delay uses straight-line NOP instructions. Do not use a loop with a
// volatile counter: the compiler sets its duration, and that duration is not
// a known cycle count. Four NOP instructions take approximately 27 ns at
// 150 MHz.
//
// A slower clock was measured. It did not reduce the rate of shifted reads,
// and it did exceed the per-pixel budget.


// defines the SPI clock frequency
// Straight-line NOPs per SCLK half period.
//
// Must stay straight-line. A loop's duration is set by the compiler, not by
// the count. That is ISSUES #8: a volatile loop counter forced a memory round
// trip per iteration and every transfer overran its budget.
//
// The NOPs are only part of the bit time. The GPIO writes, the shift and the
// call overhead make up the rest - roughly 28 cycles per bit at 4 NOPs, of
// which 8 are NOPs. So doubling this number does NOT halve the clock. Adding
// about 14 per half period does.
//
//   4  -> ~28 cycles/bit, ~187 ns/bit, maxread ~3.5 us   (previous default)
//   18 -> ~56 cycles/bit, ~373 ns/bit, maxread ~7 us     (half the rate)
//
// maxread is the measurement. Change this, watch that number, and confirm it
// moved the way you expected. Budget: the read starts at readDelayUs (6 us)
// and must finish before the next conversion at 20 us, so it has 14 us.
#define SCLK_HALF_PERIOD_NOPS 4
#define SCLK_STR2(x) #x
#define SCLK_STR(x) SCLK_STR2(x)

void __not_in_flash_func(adcClockDelay)() { // keep this function in RAM for faster retrieval
  if (g_sclkDelayNops == 0) {
    __asm__ volatile(".rept " SCLK_STR(SCLK_HALF_PERIOD_NOPS) "\n\tnop\n\t.endr\n\t");
    return;
  }

  // only used for debug. potentially remove TODO
  for (uint32_t i = 0; i < g_sclkDelayNops; ++i) {
    __asm__ volatile("nop");
  }
}

// ADS7279 requires at least 5 ns from CS low to the first falling SCLK edge.
// Four NOPs provide about 27 ns at 150 MHz.
void __not_in_flash_func(adcCsSetupDelay)() {
  __asm__ volatile(
      "nop\n\t nop\n\t nop\n\t nop\n\t"
    );
}

// Reads one pixel: one 16-bit transfer, once.
//
// This function does not drive MOSI. It therefore sends the command that the
// idle MOSI level encodes, which is the ADS7279's default mode.
//
// The ADS7279 puts the MSB on SDO when CS falls. Each following bit appears
// on each falling SCLK edge. Interrupts stay masked for the full transfer, so
// nothing can stretch the loop across an edge.
//
// The loop uses the SIO registers directly. gpio_put() is too slow, because
// its call overhead dominated this loop.
//
// WHY ONE READ (removed 2026-08-28)
// This used to read twice and take a median of three on disagreement, to
// catch a transfer losing or gaining an SPI clock. That fault returns exactly
// half or exactly twice the true value and a single read cannot identify it.
//
// The double read only works if both reads see the SAME conversion, and they
// stopped doing so. Measured: all three reads disagreed on 995 of 1022
// pixels. The ADS7279 holds a static result on SDO, so three different values
// means three different conversions - the trigger net was producing roughly
// six conversions inside each 20 us pixel, each sampling the video line at a
// different point in its CDS waveform, and a 3 to 4 us transfer straddled
// them. The median was selecting arbitrarily among those, not rejecting
// corruption, and it cost three transfers per pixel: 12 us of a 20 us budget,
// which drove maxread to 35 us and lost 1 to 3 pixels per frame.
//
// Restore the double read when the trigger net stops producing extra
// conversions. The evidence to re-check is whether repeated reads of one
// conversion agree; they can only serve as a corruption check if they do.
uint16_t __not_in_flash_func(adcReadPixel)() {
  constexpr uint32_t SCLK_MASK = 1u << PIN_ADC_SCLK; // bitmask for driving gpio. leftshifts "1" by the pin number
  constexpr uint32_t MISO_MASK = 1u << PIN_ADC_MISO; 

  uint16_t word = 0;
  const uint32_t irqState = save_and_disable_interrupts(); // store previous interrupt state so we can restore

  // set SCLK and CS low. 
  sio_hw->gpio_clr = SCLK_MASK;
  sio_hw->gpio_clr = (1u << PIN_ADC_CS);
  adcCsSetupDelay(); // add in that delay

  // for each bit we want to read:
  for (int bit = 0; bit < 16; ++bit) {
    // SCLK high
    sio_hw->gpio_set = SCLK_MASK;  // rising edge: current bit is stable
    adcClockDelay(); // wait
    // shift word left by 1 bit, and then we read the value on MISO and add to word
    word = (uint16_t)((word << 1) | ((sio_hw->gpio_in & MISO_MASK) ? 1u : 0u)); 
    // sclk low
    sio_hw->gpio_clr = SCLK_MASK;  // falling edge: next bit shifts out
    adcClockDelay();
  }

  // debug lines remove? todo
  g_triggerHighAtReadEnd = gpio_get(PIN_PIXEL_TRIG);
  g_lastReadLowBits = (uint8_t)(word & 0x3);
  g_lastRawWord = word;

  // reassert CS and interrupt states. return the word
  gpio_put(PIN_ADC_CS, true);
  restore_interrupts(irqState);
  return word;
}


// Does one 16-bit transfer and drives SDI with txWord.

// sends a command txWord over spi to the adc
uint16_t adcCommand(uint16_t txWord) {
  uint16_t rxWord = 0; // the recieved data
  const uint32_t irqState = save_and_disable_interrupts(); // save interrupt state

  gpio_put(PIN_ADC_SCLK, false); // set sclk low
  gpio_put(PIN_ADC_MOSI, (txWord & 0x8000) != 0);  // MSB is set before CS falls
  gpio_put(PIN_ADC_CS, false); // pull CS low
  adcCsSetupDelay(); // wait

  // for each bit that we send
  for (int bit = 0; bit < 16; ++bit) {
    // pull SCLK high
    gpio_put(PIN_ADC_SCLK, true);
    adcClockDelay();
    
    // SPI is full duplex so for every bit we send to the ADC, the ADC sends one back
    
    // record the bit we recieved in response to the previously transmitted bit
    rxWord = (uint16_t)((rxWord << 1) | (gpio_get(PIN_ADC_MISO) ? 1u : 0u));
    gpio_put(PIN_ADC_SCLK, false); // clock SCLK
    adcClockDelay();
    // send the next transmitted bit
    const int nextBit = bit + 1;
    if (nextBit < 16) {
      gpio_put(PIN_ADC_MOSI, (txWord & (0x8000 >> nextBit)) != 0);
      // Set the SDI bit before the next SCLK falling edge that samples the SDI bit
    }
  }

  // when done restore CS, MOSI, interrupts. return the recieved data.
  gpio_put(PIN_ADC_CS, true);
  gpio_put(PIN_ADC_MOSI, true);  // return to the default-mode idle level
  restore_interrupts(irqState);
  return rxWord;
}


// What one software reset did, so the caller can report whether it worked.
struct AdcResetResult {
  uint16_t before;      // CFR before anything was written
  uint16_t armedWrote;  // the off-default value written to arm the test
  uint16_t armedRead;   // what came back; equal to armedWrote when SDI works
  uint16_t postWord;    // the full transfer that followed the reset
  uint16_t after;       // CFR after the reset; the default when it worked
  bool armed;           // the CFR really moved off its default
  bool restored;        // the CFR came back to its default on its own
  bool postWordOk;      // the documented post-reset SDO signature appeared
};

// Software reset of the ADC, with proof that it happened.
//
// Writing the reset bit proves nothing on its own. A CFR read after the reset
// proves nothing either, because the register reads its default whether or
// not the reset ran. So this first moves the CFR OFF its default, then resets,
// then checks that the register returned by itself. Only a reset does that.
//
// D11 is the arming bit. It selects manual or auto channel scanning, which
// applies to the two-channel ADS7280. The ADS7279 has one channel, so moving
// the bit changes no behaviour here. Every other writable bit moves the
// conversion clock, the trigger source, the EOC pin or a power-down mode, and
// each of those would disturb a running scan.
//
// The reset itself sets every register back to the defaults, all ones, and
// resets the state machine to the power-on state. So D0 returns to 1 on its
// own and the second write does not have to clear it.
//
// The second write is still required, for a different reason: the datasheet
// states that SDO outputs all ones for the ONE cycle immediately after a
// reset. The second transfer consumes that cycle. Without it, the next pixel
// read returns ADC_POST_RESET_WORD, whose 14 result bits are all ones - full
// scale, a bright spike in the image that looks like real data. That same
// transfer is the second signature this function reports, in postWord.
//
// The 50 us delay is not a datasheet number. No reset settling time is
// specified.
//
// Nothing may come between the write and the readback. A pixel read leaves
// SDI high, which is command 1111b, and that command reloads the CFR
// defaults. Every transfer here happens back to back, so nothing can.
AdcResetResult adcSoftwareResetVerified() {
  AdcResetResult r;

  r.before = adcCommand(ADC_CMD_READ_CFR) & ADC_CFR_MASK;

  // Arm: move the CFR somewhere only a reset puts back.
  r.armedWrote = (uint16_t)(ADC_CFR_NORMAL & ~0x800);  // D11 low
  adcCommand(ADC_CMD_WRITE_CFR | r.armedWrote);
  r.armedRead = adcCommand(ADC_CMD_READ_CFR) & ADC_CFR_MASK;
  r.armed = ((r.armedRead & ~ADC_CFR_IGNORED) ==
             (uint16_t)(r.armedWrote & ~ADC_CFR_IGNORED));

  // Reset.
  adcCommand(ADC_CMD_WRITE_CFR | ADC_CFR_RESET);
  delayMicroseconds(50);
  r.postWord = adcCommand(ADC_CMD_WRITE_CFR | ADC_CFR_NORMAL);
  r.postWordOk = (r.postWord == ADC_POST_RESET_WORD);

  // Verify.
  r.after = adcCommand(ADC_CMD_READ_CFR) & ADC_CFR_MASK;
  r.restored = ((r.after & ~ADC_CFR_IGNORED) == ADC_CFR_DEFAULT);
  return r;
}

// Resets the ADC and discards the evidence. For callers that only need the
// side effect.
void adcSoftwareReset() { (void)adcSoftwareResetVerified(); }

// ---------------------------------------------------------------------------
// SPI self check
//
// Proves the SDI direction end to end. A pixel read cannot: adcReadPixel()
// does not drive SDI at all, so every frame captured proves SDO works and
// proves nothing whatever about SDI. An SDI fault is silent. The line stuck
// high sends command 1111b, default mode, which returns plausible conversion
// data to every command, so the instrument keeps producing spectra that look
// entirely normal and are not the spectra it was asked for.
//
// TEST 1, THE COMMAND FIELD, D[15:12].
// SDO normally carries conversion data whenever CS is low. The one exception
// is a CFR read: that command replaces the low 12 bits with the register
// contents. The two cases are told apart by the last two bits.
//
//   conversion data - 14 result bits then 00.   So bits[1:0] = 00.
//   CFR contents    - D1 then D0. The ADS7279 ignores D1 and reads it 0,
//                     and D0 reads 1 for normal operation. So bits[1:0] = 01.
//
// Sending C000h and getting bits[1:0] = 01 proves all four command bits
// arrived, because any single wrong bit is a different command and none of
// the others produce CFR contents.
//
// TEST 2, THE DATA FIELD, D[11:0].
// Writes D11 low, reads it back, then writes it high and reads it back. That
// carries a 0 and a 1 through a data bit and confirms both.
//
// D11 is the only CFR bit that is safe to move on this instrument. It selects
// manual or auto channel scanning, which applies to the two-channel ADS7280.
// The ADS7279 has one channel, so the bit changes no behaviour here. Every
// other writable bit alters the conversion clock, the trigger source, the EOC
// pin or a power-down mode, and each of those would disturb a running scan.
//
// Nothing may come between a write and its readback. A pixel read leaves SDI
// high, which is command 1111b, and that command reloads the CFR defaults.
// Every transfer here happens back to back inside this function, so nothing
// can.
//
// This is one function rather than a block inside the "sditest" handler
// because the boot check and the command both run it. Two copies of a test
// are two tests that can disagree about what passing means.
// ---------------------------------------------------------------------------

struct AdcSpiTestResult {
  uint16_t cfrWord;        // the read-CFR transfer
  uint16_t dataWord;       // the read-data transfer
  bool commandFieldOk;     // both of the above carried the right low 2 bits
  uint16_t wrote[2];       // D11 low, then D11 high
  uint16_t readback[2];
  bool dataFieldOk;        // both readbacks matched
  uint32_t failures;
  bool pass;
};

AdcSpiTestResult adcSpiSelfTest() {
  AdcSpiTestResult r;
  r.failures = 0;

  // Test 1.
  r.cfrWord = adcCommand(ADC_CMD_READ_CFR);
  r.dataWord = adcCommand(ADC_CMD_READ_DATA);
  r.commandFieldOk = ((r.cfrWord & 0x3) == 0x1) && ((r.dataWord & 0x3) == 0x0);
  if (!r.commandFieldOk) ++r.failures;

  // Test 2. ADC_CFR_IGNORED masks the two bits the part does not implement,
  // so they cannot fail the comparison.
  const uint16_t patterns[2] = {(uint16_t)(ADC_CFR_NORMAL & ~0x800),
                                ADC_CFR_NORMAL};
  r.dataFieldOk = true;
  for (int i = 0; i < 2; ++i) {
    r.wrote[i] = patterns[i];
    adcCommand(ADC_CMD_WRITE_CFR | r.wrote[i]);
    r.readback[i] = adcCommand(ADC_CMD_READ_CFR) & ADC_CFR_MASK;
    const uint16_t expect = (uint16_t)(r.wrote[i] & ~ADC_CFR_IGNORED);
    if ((r.readback[i] & ~ADC_CFR_IGNORED) != expect) {
      r.dataFieldOk = false;
      ++r.failures;
    }
  }

  // Leave the CFR at its defaults, which is the state a pixel read wants.
  adcCommand(ADC_CMD_WRITE_CFR | ADC_CFR_NORMAL);
  r.pass = (r.failures == 0);
  return r;
}

// The "H sditest" lines. Kept apart from the test itself so that the boot
// check and the command report identically.
void emitSpiSelfTest(const AdcSpiTestResult &r) {
  char line[144];
  snprintf(line, sizeof(line),
           "H sditest cmdfield readcfr=%04X low2=%u readdata=%04X low2=%u %s",
           (unsigned)r.cfrWord, (unsigned)(r.cfrWord & 0x3),
           (unsigned)r.dataWord, (unsigned)(r.dataWord & 0x3),
           r.commandFieldOk ? "PASS" : "FAIL");
  txLine(line);

  for (int i = 0; i < 2; ++i) {
    const uint16_t expect = (uint16_t)(r.wrote[i] & ~ADC_CFR_IGNORED);
    const bool ok = ((r.readback[i] & ~ADC_CFR_IGNORED) == expect);
    snprintf(line, sizeof(line),
             "H sditest datafield d11=%d wrote=%03X readback=%03X "
             "expect=%03X %s",
             (int)((r.wrote[i] >> 11) & 1), (unsigned)r.wrote[i],
             (unsigned)r.readback[i], (unsigned)expect, ok ? "PASS" : "FAIL");
    txLine(line);
  }

  snprintf(line, sizeof(line), "H sditest=%s failures=%lu",
           r.pass ? "PASS" : "FAIL", (unsigned long)r.failures);
  txLine(line);
}

// The "H adcreset" lines, for the same reason.
void emitAdcReset(const AdcResetResult &r) {
  char line[144];

  snprintf(line, sizeof(line), "H adcreset armed wrote=%03X readback=%03X %s",
           (unsigned)r.armedWrote, (unsigned)r.armedRead,
           r.armed ? "PASS" : "FAIL_sdi_write");
  txLine(line);

  snprintf(line, sizeof(line), "H adcreset postreset_sdo=%04X expect=%04X %s",
           (unsigned)r.postWord, (unsigned)ADC_POST_RESET_WORD,
           r.postWordOk ? "PASS" : "FAIL");
  txLine(line);

  snprintf(line, sizeof(line),
           "H adcreset restored before=%03X after=%03X expect=%03X %s",
           (unsigned)r.before, (unsigned)r.after, (unsigned)ADC_CFR_DEFAULT,
           !r.armed ? "inconclusive_not_armed"
                    : (r.restored ? "PASS" : "FAIL"));
  txLine(line);

  snprintf(line, sizeof(line), "H adcreset=%s",
           (r.armed && r.restored && r.postWordOk) ? "PASS" : "FAIL");
  txLine(line);
}

// The SPI self check, then the ADC reset, in that order, with both results
// reported. Run at boot and by the "selftest" command.
//
// The order matters. The self check leaves the CFR at its defaults but ends
// wherever the part happened to be otherwise, and it has just written the
// register twice; the reset that follows puts every register and the state
// machine back to the power-on state and consumes the all-ones cycle the
// datasheet says SDO produces immediately afterwards. So the sequence ends in
// exactly the state the first pixel read wants, which the reverse order would
// not.
//
// Running it the other way round would also test less. If SDI is dead, the
// reset cannot be armed and reports "inconclusive_not_armed", which says
// nothing about why. The self check ahead of it names the fault.
bool g_selfTestPass = false;    // reported in the "I " line, see emitDeviceInfo

bool runSelfTest() {
  const AdcSpiTestResult spi = adcSpiSelfTest();
  emitSpiSelfTest(spi);
  const AdcResetResult reset = adcSoftwareResetVerified();
  emitAdcReset(reset);

  const bool pass = spi.pass && reset.armed && reset.restored &&
                    reset.postWordOk;
  g_selfTestPass = pass;
  char line[96];
  snprintf(line, sizeof(line), "H selftest=%s spi=%s adcreset=%s",
           pass ? "PASS" : "FAIL", spi.pass ? "PASS" : "FAIL",
           (reset.armed && reset.restored && reset.postWordOk) ? "PASS"
                                                               : "FAIL");
  txLine(line);
  return pass;
}

// ===========================================================================
// Pixel trigger detection (see pixel_trig.pio)
//
// Two software designs lost real pixels before this one.
//
// An interrupt on a falling edge can merge two edges into one entry. The
// RP2350 edge detector is one sticky bit. It is not a counter.
//
// A CPU poll can miss a pulse completely. Any other interrupt stops the poll
// loop, and a level read has no memory of an edge that occurred during that
// time.
//
// The ARM core cannot stop a PIO state machine. The state machine also
// reports through a FIFO of depth 8, not through one bit. A short CPU delay
// therefore costs no data.
// ===========================================================================

struct PioResources {
  PIO pixelTrigPio = pio0;
  int pixelTrigSm = -1;
  int rawEdgeSm = -1;
};

PioResources pioResources;

PIO &g_pixelTrigPio = pioResources.pixelTrigPio;
int &g_pixelTrigSm = pioResources.pixelTrigSm;
int &g_rawEdgeSm = pioResources.rawEdgeSm;

// Returns the state machine's wait count for this trigger. See
// triggerIntervalUs(). The word used to be a placeholder bit and was
// discarded; callers that do not need the interval may still ignore it.
uint32_t countFilteredTrigger() {
  const uint32_t waitCounts = pio_sm_get(g_pixelTrigPio, g_pixelTrigSm);
  const uint32_t nowUs = micros();

  // Events at index > PIXEL_COUNT fall after the frame's real pixels. Their
  // spacing tells us whether they are one periodic source (min ~= max ~=
  // pixel period) or irregular bounce (min and max far apart).
  if (filteredTriggerCount.eventCount >= PIXEL_COUNT) {
    const uint32_t gapUs = nowUs - filteredTriggerCount.lastEventUs;
    if (filteredTriggerCount.eventCount == PIXEL_COUNT) {
      filteredTriggerCount.firstExtraGapUs = gapUs;
    }
    if (gapUs < filteredTriggerCount.minExtraGapUs) filteredTriggerCount.minExtraGapUs = gapUs;
    if (gapUs > filteredTriggerCount.maxExtraGapUs) filteredTriggerCount.maxExtraGapUs = gapUs;
  }

  filteredTriggerCount.lastEventUs = nowUs;
  ++filteredTriggerCount.eventCount;
  return waitCounts;
}

void drainFilteredTriggers() {
  while (!pio_sm_is_rx_fifo_empty(g_pixelTrigPio, g_pixelTrigSm)) {
    (void)countFilteredTrigger();
  }
}

// Diagnostic line for the live stream. One per frame, about 110 bytes.
//
// LIVE only. It used to run in every mode, which broke two things at once: a
// capture is supposed to transmit nothing at all, and this was sending 16 KB
// over a ten second one; and during a result transfer it was landing inside
// the record. The mode test is the fix for both. See g_txBinaryLock.
void reportFilteredTriggerCount() {
  if (g_mode != MODE_LIVE) {
    return;
  }
  if (!filteredTriggerCount.active) {
    return;
  }

  const int32_t delta =
      (int32_t)filteredTriggerCount.eventCount - (int32_t)PIXEL_COUNT;
  char line[160];
  snprintf(line, sizeof(line),
           "T frame %lu filtered %lu expected %lu delta %+ld "
           "extrafirst %luus extragap %lu-%luus",
           (unsigned long)filteredTriggerCount.frameSequence,
           (unsigned long)filteredTriggerCount.eventCount,
           (unsigned long)PIXEL_COUNT, (long)delta,
           (unsigned long)filteredTriggerCount.firstExtraGapUs,
           (unsigned long)(filteredTriggerCount.minExtraGapUs == UINT32_MAX
                                ? 0
                                : filteredTriggerCount.minExtraGapUs),
           (unsigned long)filteredTriggerCount.maxExtraGapUs);
  txLine(line);
}

// Converts REFRACTORY_US to PIO cycles.
uint32_t refractoryCycles() {
  const uint32_t cyclesPerUs = clock_get_hz(clk_sys) / 1000000u;
  return (REFRACTORY_US * cyclesPerUs) - 1u;
  // -1 because if we want to count down from n to 0 inclusive, we loop n-1 times.
}

// Cached at init so the capture loop does not call clock_get_hz() per pixel.
uint32_t g_refractoryCycles = 0;
uint32_t g_cyclesPerUs = 1;

// Converts a FIFO word into the interval since the previous accepted trigger.
//
// The state machine counts only the time it spends WATCHING the pin: two
// cycles per pass through its arm/watch loops. It cannot count the refractory
// blackout, because during the blackout it is not looking at the pin at all.
// The blackout is a known constant, so the CPU adds it back here.
//
// This number is measured before the CPU sees the word, so no interrupt, no
// Serial write and no scheduling delay can distort it. That is the whole
// reason it exists: micros() at pop time could not tell the difference
// between a trigger that had just arrived and one that had been queued.
static inline uint32_t triggerIntervalUs(uint32_t waitCounts) {
  if (waitCounts > (UINT32_MAX / 4u)) {
    return PIXEL_PERIOD_MAX_US;  // absurd; let indexStepsForGap() clamp it
  }

  // Ten cycles per trigger sit outside both counting loops and must be added
  // back by hand: mov isr / push / mov y, the blackout's fall-through pass,
  // the three instructions at "top", the hop through "arm", and the two-cycle
  // exit from "watch". Without them a true 20.0 us period measures 19.9.
  constexpr uint32_t FIXED_CYCLES = 10u;

  const uint32_t cycles = g_refractoryCycles + FIXED_CYCLES + (2u * waitCounts);

  // Round, do not truncate. A 20.0 us period lands near 3000 cycles, which
  // truncation reports as 19 and which then becomes the period estimate.
  return (cycles + (g_cyclesPerUs / 2u)) / g_cyclesPerUs;
}

// Set by initPixelTrigPio(). See the comment there: 1 means the refractory
// cycle count reached the state machine, 0 means it did not.
uint32_t g_refractoryTxLevel = 0;

void initPixelTrigPio() {
  g_pixelTrigSm = pio_claim_unused_sm(g_pixelTrigPio, true); // assign an unused state machine ID to pixelTrigPio. abort if no SM available
  const uint offset = pio_add_program(g_pixelTrigPio, &pixel_trig_program); // loads compiled pixel_trig program into the memory of the PIO
  // offset is an automatically computed memory address for the start of the program

  // we import the pio file which brings with it a data structure containing the program.
  // we load the data from the address of that struct into the sm

  pio_sm_config cfg = pixel_trig_program_get_default_config(offset); // tells the piosm what the end of the program is. creates a config structure
  sm_config_set_in_pins(&cfg, PIN_PIXEL_TRIG); // defines the piosm input pins
  sm_config_set_jmp_pin(&cfg, PIN_PIXEL_TRIG); // defines a pin that the pio can use in a conditional jump. if high jump to label else cont.

  sm_config_set_clkdiv(&cfg, 1.0f); // set sm to have no clock division so it runs at full 150MHz ie one sample every 6.67ns
  sm_config_set_in_shift(&cfg, false, false, 32); // initializes the input shift registers. do not shift input bits right, do not autopush data to fifo, push threshold of 32
  // NOT PIO_FIFO_JOIN_RX. That join gives an RX FIFO of depth 8 by DISABLING
  // the TX FIFO, and the refractory cycle count below arrives through the TX
  // FIFO. With the join set, pio_sm_put() writes into nothing, the program's
  // "pull noblock" finds the FIFO empty and copies X into OSR, and X is 0.
  // The refractory window is then zero cycles and the state machine passes
  // every edge, including bounce. There is no error anywhere: it looks
  // exactly like working code. See ISSUES.md.
  //
  // RX depth drops from 8 to 4. That costs nothing. With a working refractory
  // the level must never exceed 1, and a deep backlog is already a fault:
  // a backlogged timestamp no longer reflects arrival time, which disables
  // the positional indexing in runScan().
  sm_config_set_fifo_join(&cfg, PIO_FIFO_JOIN_NONE);

  pio_sm_init(g_pixelTrigPio, g_pixelTrigSm, offset, &cfg); // initialize the sm

  // Load X before enabling the state machine, and do not leave the load to
  // the program's own "pull noblock". Execute the pull here, so that the
  // value is in X before the first instruction runs and a failed load is
  // visible instead of silent.
  g_refractoryCycles = refractoryCycles();
  g_cyclesPerUs = clock_get_hz(clk_sys) / 1000000u;
  pio_sm_put(g_pixelTrigPio, g_pixelTrigSm, g_refractoryCycles);

  // 1 means the value reached the TX FIFO. 0 means the FIFO is disabled and
  // the refractory window is dead again. Printed at boot; do not remove.
  g_refractoryTxLevel = pio_sm_get_tx_fifo_level(g_pixelTrigPio, g_pixelTrigSm);

  pio_sm_exec(g_pixelTrigPio, g_pixelTrigSm, pio_encode_pull(false, true)); // OSR <- TX FIFO
  pio_sm_exec(g_pixelTrigPio, g_pixelTrigSm, pio_encode_mov(pio_x, pio_osr)); // X <- OSR

  pio_sm_set_enabled(g_pixelTrigPio, g_pixelTrigSm, true); // enable sm

  char stamp[120];
  snprintf(stamp, sizeof(stamp),
           "BOOT: refractory %luus (%lu cycles) txlevel %lu (1 = loaded, 0 = DEAD)",
           (unsigned long)REFRACTORY_US, (unsigned long)refractoryCycles(),
           (unsigned long)g_refractoryTxLevel);
  Serial.println(stamp);
}

// raw edge state machine used for debugging, compare the raw edges to the debounced edges.
// initialize the raw edge state machine
void initRawEdgeCountPio() {
  g_rawEdgeSm = pio_claim_unused_sm(g_pixelTrigPio, true);
  const uint offset = pio_add_program(g_pixelTrigPio, &raw_edge_count_program);

  pio_sm_config cfg = raw_edge_count_program_get_default_config(offset);
  sm_config_set_in_pins(&cfg, PIN_PIXEL_TRIG);
  sm_config_set_clkdiv(&cfg, 1.0f);  // must not sample slower than the program it audits
  sm_config_set_in_shift(&cfg, false, false, 32);
  sm_config_set_fifo_join(&cfg, PIO_FIFO_JOIN_RX);

  pio_sm_init(g_pixelTrigPio, g_rawEdgeSm, offset, &cfg);
  pio_sm_set_enabled(g_pixelTrigPio, g_rawEdgeSm, true);
}
// empties raw edge FIFO and counts how many entries it removed
uint32_t drainRawEdgeCount() {
  uint32_t n = 0; 
  while (!pio_sm_is_rx_fifo_empty(g_pixelTrigPio, g_rawEdgeSm)) { // while the raw edge rxfifo is not empty, 
    (void)pio_sm_get(g_pixelTrigPio, g_rawEdgeSm); // remove that entry from the raw edge state machine
    ++n;
  }
  return n; // return # of entries removed from fifo
}

// Identifies the pixel_trig.pio build that runs now.
//
// pixel_trig.pio.h is a generated file. If you edit the .pio file and do not
// generate the header again, the firmware compiles and flashes with the old
// program. This checksum uses the instruction words in this binary. If you
// change the .pio file and this value does not change, your edit does not
// run.
void printPioBuildStamp() {
  uint32_t checksum = 0;
  for (uint8_t i = 0; i < pixel_trig_program.length; ++i) {
    checksum = (checksum * 31u) + pixel_trig_program_instructions[i];
  }

  char line[160];
  snprintf(line, sizeof(line),
           "BOOT: pixel_trig.pio len=%u checksum=0x%08lX clkdiv=1.0 "
           "refractory=%luus (%lu cycles @ %luMHz) built=%s_%s",
           (unsigned)pixel_trig_program.length, (unsigned long)checksum,
           (unsigned long)REFRACTORY_US, (unsigned long)refractoryCycles(),
           (unsigned long)(clock_get_hz(clk_sys) / 1000000u),
           __DATE__, __TIME__);
  Serial.println(line);
}

// ===========================================================================
// Scan
// ===========================================================================

struct ScanResult {
  uint32_t capturedCount = 0;   // pixels genuinely received and read
  uint32_t highestIndex = 0;    // highest slot reached; can exceed capturedCount
  uint32_t missingCount = 0;    // slots left as MISSING_PIXEL
  uint32_t rawEdgeCount = 0;    // unfiltered transitions, bounce included
  uint32_t minRawGapUs = UINT32_MAX;
  uint32_t maxRawGapUs = 0;
  uint32_t maxReadDurationUs = 0;
  uint32_t earlyDeassertEdges = 0;
  uint32_t maxFifoBacklog = 0;  // how close the CPU came to the FIFO limit
  uint32_t staleTriggers = 0;   // triggers whose conversion the ADC had already replaced
  uint32_t firstStaleIndex = 0; // pixel index of the first one, 0 if none
  uint32_t badBitsDropped = 0;  // pixels dropped: no two reads agreed, or bad trailing bits
  uint32_t rereadPixels = 0;    // pixels that needed a third read
  uint32_t badIndex[4] = {0, 0, 0, 0};  // where the first four drops landed
  uint32_t firstBadUs = 0;      // free-running micros() at the first drop
  uint16_t firstBadWords[3] = {0, 0, 0};  // the three reads that disagreed
  uint32_t medianGapUs = 0;     // period estimate for the NEXT frame
  uint32_t startRetries = 0;
  bool timedOut = false;
  bool neverStarted = false;    // no trigger within FIRST_TRIGGER_TIMEOUT_US
  uint16_t gapHist[GAP_HIST_BUCKETS] = {0};
  uint32_t lowBitsHist[4] = {0};
};

// calculates how many pixel positions passed between two accepted trigger events
// The gap is now measured by the state machine, so there is no longer any
// question of whether to believe it. The "trustworthy" argument this used to
// take existed only to say whether the CPU's own timestamp meant anything.
static inline uint32_t indexStepsForGap(uint32_t gapUs, uint32_t periodUs) {
  const uint32_t halfPeriodUs = periodUs / 2;
  if (gapUs < (periodUs + halfPeriodUs)) {
    return 1;
  }
  const uint32_t steps = (gapUs + halfPeriodUs) / periodUs; // add bc prevent round down for the int
  return (steps > MAX_INDEX_JUMP) ? MAX_INDEX_JUMP : steps; // if the steps > max index jump, then we return max index jump. otherwise we return steps
}

// Sets g_pixelPeriodUs from the MEDIAN gap of this frame so that we have a good idea of how long time between pixels
void updatePeriodEstimate(ScanResult &result) {
  uint32_t samples = 0;
  for (uint32_t i = 0; i < GAP_HIST_BUCKETS; ++i) {
    samples += result.gapHist[i];
  }
  if (samples < GAP_HIST_MIN_SAMPLES) {
    return;
  }

  const uint32_t halfway = samples / 2;
  uint32_t running = 0;
  for (uint32_t i = 0; i < GAP_HIST_BUCKETS; ++i) {
    running += result.gapHist[i];
    if (running >= halfway) {
      result.medianGapUs = i;
      break;
    }
  }
  if (result.medianGapUs >= PIXEL_PERIOD_MIN_US &&
      result.medianGapUs <= PIXEL_PERIOD_MAX_US) {
    g_pixelPeriodUs = result.medianGapUs;
  }
}

// Reads the trigger FIFO and fills outBuf. The PIO program debounced each
// word in the FIFO, so every word is a real trigger.
//
// POSITIONAL INDEXING: the arrival time of a trigger sets its slot. The
// number of triggers received does not set it.
//
// The two differ only when a pixel is lost, and that difference caused the
// drifting image. The earlier code used outBuf[capturedCount++]. One lost
// trigger then moved the whole rest of the frame one slot earlier. The frame
// stayed plausible, and nothing marked the error.
//
// This code advances the index by the number of periods that elapsed. A lost
// pixel therefore becomes one marked hole. The pixels after it do not move.
ScanResult runScan(uint16_t *outBuf) {
  ScanResult result;

  const uint32_t periodUs = g_pixelPeriodUs;  // snapshot: must not change during a scan
  const uint32_t watchdogUs = scanWatchdogUs(periodUs);
  uint32_t nextIndex = 0; // the next index that a trigger is expected for

  // The first word of a scan counts from when the state machine was enabled,
  // not from a previous trigger, so its interval means nothing.
  bool havePreviousTrigger = false;

  // Fill the buffer first. Each slot that receives no trigger then already
  // holds the sentinel. No second pass is necessary, and the losses do not
  // need to be contiguous.
  for (uint32_t i = 0; i < PIXEL_COUNT; ++i) {
    outBuf[i] = MISSING_PIXEL;
  }

  const uint32_t scanStartUs = micros();

  while (nextIndex < PIXEL_COUNT) {
    const uint32_t elapsedUs = (uint32_t)(micros() - scanStartUs);
    if (elapsedUs > watchdogUs) {
      result.timedOut = true;
      break;
    }
    if (result.capturedCount == 0 && elapsedUs > FIRST_TRIGGER_TIMEOUT_US) {
      result.neverStarted = true;  // runFrame() sends START again
      break;
    }

    // Read this FIFO in each pass. A backlog would overflow it and cause an
    // undercount.
    result.rawEdgeCount += drainRawEdgeCount();

    const uint32_t fifoLevel =
        pio_sm_get_rx_fifo_level(g_pixelTrigPio, g_pixelTrigSm);
    if (fifoLevel == 0) {
      continue;
    }
    if (fifoLevel > result.maxFifoBacklog) {
      result.maxFifoBacklog = fifoLevel;
    }

    // A backlog of two or more means the ADC has already moved on.
    //
    // The ADS7279 holds exactly ONE result on SDO, from the end of a
    // conversion until the next one starts. So when two triggers are queued,
    // the conversion belonging to the older one no longer exists anywhere.
    // Popping it and reading SDO returns the NEWER pixel, and filing that
    // value under the older trigger's index stores the wrong pixel in that
    // slot. The value is a perfectly plausible reading, so nothing
    // downstream can detect it. The following pass then reads the same held
    // value again and stores a duplicate. One backlog of 2 therefore costs
    // one pixel AND silently corrupts a second.
    //
    // Discard every queued trigger except the newest and leave their slots
    // at MISSING_PIXEL. A marked hole is recoverable; a plausible wrong
    // value is not. See the recurring lessons in ISSUES.md.
    uint32_t backlog = fifoLevel;
    const bool hadBacklog = (backlog > 1);
    while (backlog > 1 && nextIndex < PIXEL_COUNT) {
      (void)countFilteredTrigger();   // it was a real trigger, so it still counts
      if (result.staleTriggers == 0) {
        result.firstStaleIndex = nextIndex;
      }
      ++result.staleTriggers;
      ++result.missingCount;
      ++nextIndex;              // this slot keeps its MISSING_PIXEL sentinel
      --backlog;
    }
    if (nextIndex >= PIXEL_COUNT) {
      break;
    }
    if (hadBacklog) {
      // The surviving trigger may still be converting: the state machine
      // pushes on the FALLING edge, which is the START of a conversion.
      // Reading now would return the conversion before it, which is exactly
      // the corruption this block exists to prevent. Wait, bounded, for the
      // line to go high again - that transition is the conversion finishing.
      //
      // This rule is not specific to the backlog path. Every read wants it,
      // and applying it to every read is the proper fix for the stale first
      // read described above adcReadPixel(). It is applied only here for now
      // to keep this change contained.
      const uint32_t waitStartUs = micros();
      while (!gpio_get(PIN_PIXEL_TRIG) &&
             (uint32_t)(micros() - waitStartUs) < (PIXEL_PERIOD_NOMINAL_US / 2)) {
      }
    }

    const uint32_t waitCounts = countFilteredTrigger();

    uint32_t index = nextIndex;

    if (havePreviousTrigger) {
      // Measured by the state machine, not by micros() at pop time. This is
      // the real interval between accepted triggers on the pin.
      const uint32_t gapUs = triggerIntervalUs(waitCounts);
      if (gapUs > result.maxRawGapUs) result.maxRawGapUs = gapUs;
      if (gapUs < result.minRawGapUs) result.minRawGapUs = gapUs;

      const uint32_t steps = indexStepsForGap(gapUs, periodUs);

      // Give the estimator only clean single-period gaps. A gap that spans a
      // real hole would increase the estimate.
      if (steps == 1) {
        const uint32_t bucket =
            (gapUs < GAP_HIST_BUCKETS) ? gapUs : (GAP_HIST_BUCKETS - 1);
        ++result.gapHist[bucket];
      }

      index = (nextIndex - 1) + steps;  // nextIndex-1 is the slot just filled
      if (index >= PIXEL_COUNT) {
        break;  // this trigger belongs after the end of the frame
      }
      result.missingCount += (steps - 1);
    }
    havePreviousTrigger = true;

    // Diagnostic only. A delay here lets the conversion finish before CS
    // latches the output register. See adcReadPixel().
    if (g_readDelayUs != 0) {
      delayMicroseconds(g_readDelayUs);
    }
    // Mask across the timestamps, not just the transfer. adcReadPixel()
    // masks its own 16 bits, but interrupts are live between micros() and
    // that mask, and again between the unmask and the second micros(). A USB
    // interrupt landing in either window is counted in full and delays the
    // read relative to the conversion. Measured: 6-7us typical with outliers
    // at 21-26us, which is TinyUSB service time. Nesting is safe:
    // adcReadPixel() saves the already-disabled state and restores it.
    // Read the same conversion twice and require the raw words to match.
    //
    // The fault being caught is a lost or gained SPI clock. It shifts the
    // 16-bit window against the ADC's bit stream, so the value comes back
    // exactly doubled or exactly halved - a plausible reading that no single
    // read can identify. Measured 2026-08-28: both directions occur, roughly
    // once every 10 frames each.
    //
    // A slip is a random event, so two transfers slipping identically is
    // vanishingly unlikely. Any disagreement is corruption, in either
    // direction. That is the only test that sees both: the trailing-bits
    // check below catches only the halving direction, because a window that
    // shifts further into the all-zero trailing region leaves no trace.
    //
    // This only works because both reads see the SAME conversion. It did not
    // before readDelayUs was introduced: the reads were landing at the start
    // of the conversion and straddling the moment SDO is loaded, so they
    // disagreed on 995 of 1022 pixels and the comparison meant nothing. With
    // the read placed after EOC rises, the result is static on SDO until the
    // next CONVST 20 us later.
    //
    // Budget: 6 us delay + two 4 us reads = 14 us of 20 us. The third read
    // only happens on a mismatch and takes it to 18 us, which is tight; if
    // that starts costing pixels it will show up in "stale".
    const uint32_t readIrq = save_and_disable_interrupts();
    const uint32_t readStartUs = micros();
    const uint16_t firstWord = adcReadPixel();
    const uint16_t secondWord = adcReadPixel();
    uint16_t word = secondWord;
    uint16_t thirdWord = 0;
    bool wordConfirmed = (firstWord == secondWord);
    if (!wordConfirmed) {
      thirdWord = adcReadPixel();
      ++result.rereadPixels;
      word = thirdWord;
      wordConfirmed = (thirdWord == firstWord) || (thirdWord == secondWord);
    }
    const uint32_t readDurationUs = (uint32_t)(micros() - readStartUs);
    restore_interrupts(readIrq);
    if (readDurationUs > result.maxReadDurationUs) {
      result.maxReadDurationUs = readDurationUs;
    }
    if (g_triggerHighAtReadEnd) {
      ++result.earlyDeassertEdges;
    }
    ++result.lowBitsHist[g_lastReadLowBits & 0x3];

    // Drop any transfer whose trailing bits are wrong.
    //
    // The ADS7279 sends 14 data bits and then drives zeros, so a correctly
    // framed 16-bit read always ends in 00. Anything else means the window we
    // captured was misaligned against the ADC's bit stream by at least one
    // bit - a lost or gained SPI clock - and the 14 bits above it are not the
    // conversion result. Mark the slot missing rather than store a plausible
    // wrong value.
    //
    // KNOWN LIMIT: this only sees one of the two slip directions. A window
    // that starts one bit LATE captures D12..D0 followed by three trailing
    // zeros: the value doubles and the low two bits still read 00, so it
    // passes this check. Only a window that lags, pulling a data bit down
    // into the trailing position, is caught - and that one halves the value.
    // The trailing region is all zeros, so shifting further into it leaves no
    // trace to test against, and the part's TAG bit that would sit at the
    // 17th clock is ignored on the ADS7279 and always reads 0.
    //
    // Detecting both directions needs two reads of the same conversion
    // compared bit for bit. This is here first to find out whether the
    // visible spikes and the lowbits events are the same fault or two.
    if (!wordConfirmed || (word & 0x3u) != 0u) {
      // Capture the first one: where it happened and what the three reads
      // actually were. Whether they differ by a single bit, by a one-bit
      // shift, or wildly says which fault this is, and the index says
      // whether the clustering at the start of the frame is real.
      // The spacing between drops is what identifies the interferer. badat
      // alone showed a clean +2.25 pixel per frame ramp sawtoothing over ~48
      // pixels, which is a beat against some other clock; the spacing gives
      // its period. Clustered means one burst walking through the frame,
      // ~50 apart means something recurring every 1 ms.
      if (result.badBitsDropped < 4) {
        result.badIndex[result.badBitsDropped] = index;
      }
      if (result.badBitsDropped == 0) {
        // Absolute, not relative to the frame. The position within the frame
        // drifts, and the drift rate changes with the frame rate, so the
        // interferer runs on its own clock. Differences between consecutive
        // values of this give its period directly, with no modular guessing:
        // subtract, and the answer is either the period or a multiple of it.
        result.firstBadUs = micros();
        result.firstBadWords[0] = firstWord;
        result.firstBadWords[1] = secondWord;
        result.firstBadWords[2] = thirdWord;
      }
      ++result.badBitsDropped;
      ++result.missingCount;   // slot keeps its MISSING_PIXEL sentinel
    } else {
      outBuf[index] = adcCodeFromWord(word);
      ++result.capturedCount;
    }
    result.highestIndex = index;
    nextIndex = index + 1;
    // Not every pixel. See TX_PUMP_INTERVAL_PIXELS.
    if ((result.capturedCount % TX_PUMP_INTERVAL_PIXELS) == 0) {
      pumpTx();  // continue to drain the previous frame between pixels
    }
  }

  // The loop stops when the last pixel arrives, before the drain in that
  // pass. Without this call, the raw count reads one too low.
  result.rawEdgeCount += drainRawEdgeCount();

  if (result.capturedCount > 0) {
    result.missingCount += (PIXEL_COUNT - 1) - result.highestIndex;
  } else {
    result.missingCount = PIXEL_COUNT;
  }

  updatePeriodEstimate(result);
  return result;
}

// ===========================================================================
// On-device stacking
//
// A capture runs a fixed number of frames at one exposure, rejects per-pixel
// outliers, and averages what survives. The host receives one result record
// instead of every frame.
//
// WHY HERE AND NOT ON THE HOST
// The host already does this. sigma_clip_combine() in
// tools/spectrometer/processing.py stays the reference implementation, and
// this code is written to agree with it. Moving the work here lets the link
// stay silent for the whole capture, which removes the USB interrupts that
// cost pixels (see the flush at the top of runFrame), and is the step this
// firmware needs before it can run with no host attached at all.
//
// WHY BLOCKS AND NOT THE WHOLE CAPTURE
// Clipping needs every sample of a pixel at the same time. A frame is 2048
// bytes, so a 60 s capture at 15 fps is 1.8 MB and cannot be held. Frames are
// therefore clipped in blocks of blockFrames, and only the surviving sums
// cross from one block to the next. Memory is then fixed whatever the capture
// length is.
//
// The estimator is weaker than a clip over the whole capture, because the
// median and the deviation come from 16 samples rather than from all of them.
// That is the right trade here. The fault this rejects is a lost or gained
// SPI clock, which returns exactly double or exactly half the true value (see
// adcReadPixel), and a sample that far out is rejected by any usable
// threshold.
//
// WHAT IT DOES NOT DO
// Clipping runs per pixel ACROSS TIME. A spectral line has the same value in
// every frame, so nothing here can remove one. The precondition is a scene
// that does not change during the capture. A flickering source is clipped as
// if it were corruption.
//
// The 17.4 kHz interferer is NOT rejected, and must not be. Its per-pixel
// distribution is arcsine and spans about 1.4 standard deviations, so no
// threshold above that value touches it. Averaging removes it as the square
// root of the frame count. The notch stays on the host and is applied to the
// returned mean. The notch is linear, so notching the mean and averaging
// notched frames give nearly the same answer.
// ===========================================================================

constexpr uint32_t STACK_BLOCK_FRAMES_DEFAULT = 16;
constexpr uint32_t STACK_BLOCK_FRAMES_MAX = 32;
constexpr uint32_t STACK_BLOCK_FRAMES_MIN = 4;
constexpr uint32_t STACK_CLIP_ITERS_DEFAULT = 5;
constexpr uint32_t STACK_CLIP_ITERS_MAX = 10;

constexpr uint32_t STACK_SIGMA_X100_DEFAULT = 300;
constexpr uint32_t STACK_SIGMA_X100_MIN = 100;
constexpr uint32_t STACK_SIGMA_X100_MAX = 2000;

constexpr uint32_t CAPTURE_MAX_FRAMES = 16384;
constexpr uint32_t CAPTURE_MAX_SECONDS = 600;

// Frames discarded at the start of a capture.
//
// A capture sets its own exposure, so the interval before the first frame is
// whatever the previous setting was, not the exposure that was requested.
// That frame holds a different amount of charge from every frame after it.
// Two are dropped rather than one because the CPLD pipeline is one scan deep.
constexpr uint32_t CAPTURE_WARMUP_FRAMES = 2;

// A frame with fewer real pixels than this is not stacked at all. Positional
// indexing keeps the surviving pixels in the correct slots, so a frame with a
// few holes is still good data and the per-pixel counts handle it. A frame
// that lost half its pixels means the scan itself failed.
constexpr uint32_t CAPTURE_MIN_CAPTURED = PIXEL_COUNT / 2;

// Two blocks: one fills while the other is clipped. See stackProcessSlice()
// for why the clip is spread out rather than run when a block completes.
uint16_t g_stackBlock[2][STACK_BLOCK_FRAMES_MAX][PIXEL_COUNT];

// Everything that survives a block. Integers, not floats: the samples are
// integer codes, so the sums are exact and the variance below needs no
// rounding at all.
//
//   sum    16383 * 16384 frames   = 2.7e8,  fits uint32
//   sumSq  16383^2 * 16384 frames = 4.4e12, needs uint64
struct StackAccumulator {
  uint32_t sum[PIXEL_COUNT];
  uint64_t sumSq[PIXEL_COUNT];
  uint16_t accepted[PIXEL_COUNT];
  uint16_t rejected[PIXEL_COUNT];
};

StackAccumulator g_stack;

struct CaptureState {
  bool haveResult = false;
  bool dark = false;
  uint32_t id = 0;
  uint32_t framesRequested = 0;
  uint32_t framesAttempted = 0;
  uint32_t framesStacked = 0;
  uint32_t framesExcluded = 0;
  uint32_t exposureUs = 0;
  uint32_t warmupLeft = 0;
  uint32_t startUs = 0;
  uint32_t durationMs = 0;
  uint32_t blockFrames = STACK_BLOCK_FRAMES_DEFAULT;
  uint32_t sigmaX100 = STACK_SIGMA_X100_DEFAULT;
  uint32_t clipIters = STACK_CLIP_ITERS_DEFAULT;

  // Which block buffer takes new frames, and how many it holds.
  uint8_t fillBuf = 0;
  uint32_t fillCount = 0;

  // Which block buffer is being clipped, how many frames it holds, and how
  // far through the pixels the clip has reached.
  uint8_t procBuf = 1;
  uint32_t procFrames = 0;
  uint32_t procPixel = 0;

  // Pixels clipped per frame. See stackProcessSlice().
  uint32_t sliceP = PIXEL_COUNT / STACK_BLOCK_FRAMES_DEFAULT;

  // Capture-wide diagnostics. With nothing transmitted during a capture,
  // these are the only evidence that the frames behind the result were sound.
  uint32_t statMissing = 0;
  uint32_t statStale = 0;
  uint32_t statBadBits = 0;
  uint32_t statMaxLateUs = 0;
  uint32_t statNoStart = 0;

  // Two letter name prefix for the file this capture writes to the card.
  // The host chooses it: SP spectrum, DK dark, FL flat. See the SD section.
  char sdTag[3] = "SP";

  // Restored when the capture ends, so a capture does not silently redefine
  // the live view exposure.
  uint32_t savedPeriodUs = 0;
  uint32_t savedRateHz = 0;
  uint32_t savedVerbosity = 0;
};

CaptureState capture;

// Clips one pixel across one block and adds the survivors to the totals.
//
// This is sigma_clip_combine() from the host, in integers and without the
// allocation. The host masks a stack, computes the median and the standard
// deviation again from what is still unmasked, and rejects anything further
// than sigma deviations from that median, until the mask stops changing.
//
// Sorting first makes that cheap AND identical. The accepted set is every
// sample inside a symmetric window around the median, which in a sorted array
// is one contiguous run, and the previous accepted set was also a contiguous
// run. So the whole iteration is two indices that walk inward, and the
// running sums only lose the samples that leave.
//
// There is no floating point in here at all, and that is deliberate. The
// deviation comes from m^2 * variance = m*sumSq - sum^2, and the threshold
// test is then squared so the square root never has to be taken. Every term
// is an exact integer, so the clip is exactly the clip the arithmetic
// defines.
//
// float32 was tried for the threshold and was not good enough. The bound sits
// near 8000, where float32 resolution is about 0.001 of a code, so a sample
// within a thousandth of a code of the threshold landed on the wrong side of
// it. That is rare, but each time it happened it moved a 16 sample mean by
// about 2 codes. float64 would fix it and costs about 13 us per pixel on a
// core with no double precision unit, which does not fit the per-frame slice.
//
// CHECKED, not assumed. This function was compiled natively and run against
// 4000 randomised pixels carrying doubled samples, halved samples and missing
// slots, at 1 to 10 sigma and 1 to 5 iterations. It agreed with the same
// algorithm in Python fractions on every one. It agreed with the host's
// sigma_clip_combine on all but two, and both of those are samples sitting
// exactly on the threshold, where the host is the one that is wrong: its
// float64 deviation lands a few units in the last place short of the exact
// value and rejects a sample that belongs.
void stackClipPixel(const uint16_t *block, uint32_t frames, uint32_t px) {
  uint16_t v[STACK_BLOCK_FRAMES_MAX];
  uint32_t n = 0;
  for (uint32_t f = 0; f < frames; ++f) {
    const uint16_t code = block[(f * PIXEL_COUNT) + px];
    if (code <= ADC_CODE_MASK) {  // excludes MISSING_PIXEL
      v[n++] = code;
    }
  }
  if (n == 0) {
    return;  // no trigger reached this slot in any frame of this block
  }

  for (uint32_t i = 1; i < n; ++i) {
    const uint16_t key = v[i];
    uint32_t j = i;
    while (j > 0 && v[j - 1] > key) {
      v[j] = v[j - 1];
      --j;
    }
    v[j] = key;
  }

  uint32_t lo = 0;
  uint32_t hi = n;
  int64_t s = 0;
  int64_t s2 = 0;
  for (uint32_t i = 0; i < n; ++i) {
    s += (int64_t)v[i];
    s2 += (int64_t)v[i] * (int64_t)v[i];
  }

  // No minimum sample count, and no early exit on a small surviving set.
  //
  // A guard at four survivors was tried and removed. It changed the answer on
  // 75 of 3611 randomised pixels against the host reference, in every case by
  // stopping an iteration the host went on to make. Small samples are not the
  // reason to differ from the reference: with the default block of 16 frames
  // and 3 sigma, reaching three survivors means 13 of 16 were rejected, which
  // is itself a fault worth seeing in the accepted count rather than hiding
  // behind a floor.
  //
  // Below two survivors the spread is zero and the num <= 0 test below ends
  // the loop, so no separate guard is needed for that either.
  {
    for (uint32_t iter = 0; iter < capture.clipIters; ++iter) {
      const uint32_t m = hi - lo;

      // Twice the median, so an even count keeps its half and stays an
      // integer.
      const int64_t med2 =
          (m & 1u) ? (2 * (int64_t)v[lo + (m / 2)])
                   : ((int64_t)v[lo + (m / 2) - 1] + (int64_t)v[lo + (m / 2)]);

      // Zero spread means every survivor holds the same value. The host
      // reaches the same place with np.where(std > 0, std, inf): nothing is
      // rejected and the iteration is finished.
      const int64_t num = ((int64_t)m * s2) - (s * s);  // m^2 * variance
      if (num <= 0) {
        break;
      }

      // Accept while |x - med| <= k*sd, with sd = sqrt(num)/m and
      // k = sigmaX100/100. Both sides are non-negative, so squaring keeps the
      // test, and multiplying out by (2m*100)^2 clears every denominator:
      //
      //   (2x - med2)^2 * m^2 * 10000  <=  4 * sigmaX100^2 * num
      //
      // Largest left side is 32766^2 * 32^2 * 10000 = 1.1e16, largest right
      // side is 4 * 2000^2 * 2.75e11 = 4.4e18. Both inside int64.
      const int64_t rhs = 4 * (int64_t)capture.sigmaX100 *
                          (int64_t)capture.sigmaX100 * num;
      const int64_t scale = (int64_t)m * (int64_t)m * 10000;

      uint32_t nl = lo;
      uint32_t nh = hi;
      while (nl < nh) {
        const int64_t d = (2 * (int64_t)v[nl]) - med2;
        if ((d * d * scale) <= rhs) break;
        ++nl;
      }
      while (nh > nl) {
        const int64_t d = (2 * (int64_t)v[nh - 1]) - med2;
        if ((d * d * scale) <= rhs) break;
        --nh;
      }
      if (nl == lo && nh == hi) {
        break;
      }
      for (uint32_t i = lo; i < nl; ++i) {
        s -= (int64_t)v[i];
        s2 -= (int64_t)v[i] * (int64_t)v[i];
      }
      for (uint32_t i = nh; i < hi; ++i) {
        s -= (int64_t)v[i];
        s2 -= (int64_t)v[i] * (int64_t)v[i];
      }
      lo = nl;
      hi = nh;
    }
  }

  const uint32_t kept = hi - lo;
  g_stack.sum[px] += (uint32_t)s;
  g_stack.sumSq[px] += (uint64_t)s2;
  g_stack.accepted[px] += (uint16_t)kept;
  g_stack.rejected[px] += (uint16_t)(n - kept);
}

// Clips up to maxPixels pixels of the block that is waiting to be processed.
//
// The work is spread out rather than run in one go when a block fills. A
// whole block is about 1.5 ms of clipping, and at the shortest exposures the
// gap between the end of one scan and the start of the next is only about
// 100 us. One long run there would make every sixteenth frame late.
//
// runFrame() calls this one time per frame with sliceP = PIXEL_COUNT /
// blockFrames, which finishes a block within blockFrames frames by
// construction, so the next block is never left waiting. loop() calls it
// again in the gap, which usually finishes it much sooner.
uint32_t stackProcessSlice(uint32_t maxPixels) {
  if (capture.procFrames == 0) {
    return 0;
  }
  const uint16_t *block = &g_stackBlock[capture.procBuf][0][0];
  uint32_t done = 0;
  while (capture.procPixel < PIXEL_COUNT && done < maxPixels) {
    stackClipPixel(block, capture.procFrames, capture.procPixel);
    ++capture.procPixel;
    ++done;
  }
  if (capture.procPixel >= PIXEL_COUNT) {
    capture.procFrames = 0;
  }
  return done;
}

// Hands the filled block over for clipping and starts to fill the other one.
void stackSwapBlocks() {
  // The other block should already be empty. Finish it rather than discard
  // it, in case some combination of exposure and block size starves the
  // slice.
  if (capture.procFrames != 0) {
    stackProcessSlice(PIXEL_COUNT);
  }
  capture.procBuf = capture.fillBuf;
  capture.procFrames = capture.fillCount;
  capture.procPixel = 0;
  capture.fillBuf ^= 1u;
  capture.fillCount = 0;
}

void captureBegin(uint32_t frames, uint32_t exposureUs, uint32_t sigmaX100,
                  uint32_t blockFrames, uint32_t iters, bool dark) {
  memset(g_stack.sum, 0, sizeof(g_stack.sum));
  memset(g_stack.sumSq, 0, sizeof(g_stack.sumSq));
  memset(g_stack.accepted, 0, sizeof(g_stack.accepted));
  memset(g_stack.rejected, 0, sizeof(g_stack.rejected));

  capture.savedPeriodUs = g_framePeriodUs;
  capture.savedRateHz = g_frameRateHz;
  capture.savedVerbosity = g_txVerbosity;

  ++capture.id;
  capture.haveResult = false;
  capture.dark = dark;
  capture.framesRequested = frames;
  capture.framesAttempted = 0;
  capture.framesStacked = 0;
  capture.framesExcluded = 0;
  capture.exposureUs = exposureUs;
  capture.warmupLeft = CAPTURE_WARMUP_FRAMES;
  capture.startUs = micros();
  capture.durationMs = 0;
  capture.blockFrames = blockFrames;
  capture.sigmaX100 = sigmaX100;
  capture.clipIters = iters;

  capture.fillBuf = 0;
  capture.fillCount = 0;
  capture.procBuf = 1;
  capture.procFrames = 0;
  capture.procPixel = 0;
  capture.sliceP = (PIXEL_COUNT + blockFrames - 1) / blockFrames;

  capture.statMissing = 0;
  capture.statStale = 0;
  capture.statBadBits = 0;
  capture.statMaxLateUs = 0;
  capture.statNoStart = 0;

  g_framePeriodUs = exposureUs;
  g_frameRateHz = 1000000UL / exposureUs;
  g_txVerbosity = 0;  // nothing goes out during a capture
  g_mode = MODE_CAPTURE;
}

// Defined in the SD card section, far below. captureFinish() only raises a
// flag: the write itself happens in loop(), because captureFinish() runs
// inside runFrame() and a card write can stall for tens of milliseconds.
void sdRequestSave(const char *tag);

void captureFinish(bool aborted) {
  // Clip whatever is left: the block in progress, then the partial block.
  if (capture.procFrames != 0) {
    stackProcessSlice(PIXEL_COUNT);
  }
  if (capture.fillCount > 0) {
    stackSwapBlocks();
    stackProcessSlice(PIXEL_COUNT);
  }

  capture.durationMs = (uint32_t)((micros() - capture.startUs) / 1000u);
  capture.haveResult = (capture.framesStacked > 0);

  g_framePeriodUs = capture.savedPeriodUs;
  g_frameRateHz = capture.savedRateHz;
  g_txVerbosity = capture.savedVerbosity;

  // Not LIVE. The host has to collect the result, and frames staged into the
  // same queue would share the link with it for no benefit. The host sends
  // "live 1" when it is finished.
  g_mode = MODE_QUIET;

  char line[200];
  snprintf(line, sizeof(line),
           "C state=%s id=%lu req=%lu att=%lu stack=%lu excl=%lu "
           "exp_us=%lu dur_ms=%lu result=%d",
           aborted ? "stopped" : "done", (unsigned long)capture.id,
           (unsigned long)capture.framesRequested,
           (unsigned long)capture.framesAttempted,
           (unsigned long)capture.framesStacked,
           (unsigned long)capture.framesExcluded,
           (unsigned long)capture.exposureUs,
           (unsigned long)capture.durationMs, (int)capture.haveResult);
  txLine(line);

  // Queue the card write. It runs on the next loop() pass, after the line
  // above has been handed to the host.
  if (capture.haveResult) {
    sdRequestSave(capture.sdTag);
  }
}

// Called one time per frame while a capture runs, in place of staging the
// frame for transmission.
void captureOnFrame(const ScanResult &result, uint32_t lateUs) {
  if (capture.warmupLeft > 0) {
    --capture.warmupLeft;
    return;
  }

  ++capture.framesAttempted;

  capture.statMissing += result.missingCount;
  capture.statStale += result.staleTriggers;
  capture.statBadBits += result.badBitsDropped;
  if (result.neverStarted) ++capture.statNoStart;
  if (lateUs > capture.statMaxLateUs) capture.statMaxLateUs = lateUs;

  const bool usable = !result.neverStarted &&
                      (result.capturedCount >= CAPTURE_MIN_CAPTURED);
  if (usable) {
    memcpy(&g_stackBlock[capture.fillBuf][capture.fillCount][0], pixelBuffer,
           sizeof(pixelBuffer));
    ++capture.fillCount;
    ++capture.framesStacked;
    if (capture.fillCount >= capture.blockFrames) {
      stackSwapBlocks();
    }
  } else {
    ++capture.framesExcluded;
  }

  stackProcessSlice(capture.sliceP);

  if (capture.framesAttempted >= capture.framesRequested) {
    captureFinish(false);
  }
}

// ===========================================================================
// Stack result record
//
// One binary record, sent only when the host asks for it with "cap get". The
// result stays in RAM until the next capture overwrites it, so a link that
// drops during the transfer costs a retry rather than the capture.
//
// Layout, little-endian throughout:
//
//   uint8[4]  magic  A5 5A C7 2E        (deliberately not the frame magic)
//   uint16    record version
//   uint16    flags, bit 0 = dark
//   uint32    capture id
//   uint32    frames requested
//   uint32    frames attempted
//   uint32    frames stacked
//   uint32    frames excluded
//   uint32    exposure, microseconds
//   uint32    duration, milliseconds
//   uint16    pixel count
//   uint16    block frames
//   uint16    sigma x 100
//   uint16    clip iterations
//   uint32    total missing slots
//   uint32    total stale triggers
//   uint32    total bad-bit drops
//   uint32    worst frame lateness, microseconds
//   uint32    frames that never started
//                                        (64 bytes to here)
//   per pixel, 12 bytes each:
//     float32 mean code, NaN when no sample survived
//     float32 standard deviation of the accepted samples, in codes
//     uint16  accepted samples
//     uint16  rejected samples
//   uint32    checksum, sum of every preceding byte
//
// 12356 bytes for 1024 pixels. A missing sample for a pixel is neither
// accepted nor rejected, so the host derives the count as
// frames_stacked - accepted - rejected.
//
// The standard deviation is the RAW frame-to-frame spread. On this instrument
// most of that is the 17.4 kHz interferer rather than random noise, so it is
// an upper bound on the noise and not the noise floor. See ISSUES.md entry
// 31.
// ===========================================================================

constexpr uint8_t STACK_MAGIC[4] = {0xA5, 0x5A, 0xC7, 0x2E};
constexpr uint16_t STACK_RECORD_VERSION = 1;
constexpr size_t STACK_HEADER_BYTES = 64;
constexpr size_t STACK_PIXEL_BYTES = 12;
constexpr uint32_t STACK_PIXELS_PER_CHUNK = 32;
constexpr size_t STACK_RECORD_BYTES =
    STACK_HEADER_BYTES + (STACK_PIXEL_BYTES * PIXEL_COUNT) + 4;

static inline uint8_t *putU16(uint8_t *p, uint16_t v) {
  *p++ = (uint8_t)(v & 0xFF);
  *p++ = (uint8_t)((v >> 8) & 0xFF);
  return p;
}

static inline uint8_t *putU32(uint8_t *p, uint32_t v) {
  *p++ = (uint8_t)(v & 0xFF);
  *p++ = (uint8_t)((v >> 8) & 0xFF);
  *p++ = (uint8_t)((v >> 16) & 0xFF);
  *p++ = (uint8_t)((v >> 24) & 0xFF);
  return p;
}

static inline uint8_t *putF32(uint8_t *p, float v) {
  uint32_t bits;
  memcpy(&bits, &v, sizeof(bits));
  return putU32(p, bits);
}

size_t buildStackHeader(uint8_t *out) {
  uint8_t *p = out;
  memcpy(p, STACK_MAGIC, sizeof(STACK_MAGIC));
  p += sizeof(STACK_MAGIC);
  p = putU16(p, STACK_RECORD_VERSION);
  p = putU16(p, (uint16_t)(capture.dark ? 1 : 0));
  p = putU32(p, capture.id);
  p = putU32(p, capture.framesRequested);
  p = putU32(p, capture.framesAttempted);
  p = putU32(p, capture.framesStacked);
  p = putU32(p, capture.framesExcluded);
  p = putU32(p, capture.exposureUs);
  p = putU32(p, capture.durationMs);
  p = putU16(p, (uint16_t)PIXEL_COUNT);
  p = putU16(p, (uint16_t)capture.blockFrames);
  p = putU16(p, (uint16_t)capture.sigmaX100);
  p = putU16(p, (uint16_t)capture.clipIters);
  p = putU32(p, capture.statMissing);
  p = putU32(p, capture.statStale);
  p = putU32(p, capture.statBadBits);
  p = putU32(p, capture.statMaxLateUs);
  p = putU32(p, capture.statNoStart);
  while ((size_t)(p - out) < STACK_HEADER_BYTES) {
    *p++ = 0;
  }
  return STACK_HEADER_BYTES;
}

size_t buildStackPixels(uint8_t *out, uint32_t first, uint32_t count) {
  uint8_t *p = out;
  for (uint32_t i = 0; i < count; ++i) {
    const uint32_t px = first + i;
    const uint32_t n = g_stack.accepted[px];
    float mean = NAN;
    float sd = NAN;
    if (n > 0) {
      const uint64_t sum = g_stack.sum[px];
      mean = (float)((double)sum / (double)n);
      if (n < 2) {
        sd = 0.0f;
      } else {
        // n*sumSq - sum^2 = n^2 * variance, exact in uint64. The largest
        // term is 16384 * 4.4e12 = 7.2e16, well inside the range.
        const uint64_t a = (uint64_t)n * g_stack.sumSq[px];
        const uint64_t b = sum * sum;
        const double var =
            (a > b) ? ((double)(a - b) / ((double)n * (double)n)) : 0.0;
        sd = (float)sqrt(var);
      }
    }
    p = putF32(p, mean);
    p = putF32(p, sd);
    p = putU16(p, g_stack.accepted[px]);
    p = putU16(p, g_stack.rejected[px]);
  }
  return (size_t)(p - out);
}

// A host that stops reading part way through leaves the queue full and the
// sender unable to make progress. Without a limit the lock would stay set and
// the device would be mute from then on, which is a worse failure than a lost
// transfer. Ten seconds is far longer than a transfer needs: the record is
// 12 KB and the link moves that in well under a second.
constexpr uint32_t RESULT_SEND_TIMEOUT_US = 10000000;

struct ResultSender {
  bool active = false;
  bool headerSent = false;
  uint32_t pixel = 0;
  uint32_t checksum = 0;
  uint32_t startedUs = 0;
};

ResultSender resultSender;

// Starts a transfer. Takes the lock, so nothing else can enter the queue
// until the record is complete.
void beginResultSend() {
  resultSender.active = true;
  resultSender.headerSent = false;
  resultSender.pixel = 0;
  resultSender.checksum = 0;
  resultSender.startedUs = micros();
  g_txBinaryLock = true;
}

void endResultSend() {
  resultSender.active = false;
  g_txBinaryLock = false;
}

// Feeds the result record into the transmit queue one chunk at a time.
//
// The record is three times the size of the queue, so it cannot be staged the
// way a frame is. It is generated on demand instead of being held in a
// buffer, which also keeps a second copy of it out of RAM.
void pumpResult() {
  if (!resultSender.active) {
    return;
  }
  if ((uint32_t)(micros() - resultSender.startedUs) > RESULT_SEND_TIMEOUT_US) {
    // Release the lock first, or the report of the failure is itself dropped.
    endResultSend();
    txLine("E cap send_timeout");
    return;
  }
  uint8_t buf[STACK_PIXELS_PER_CHUNK * STACK_PIXEL_BYTES];

  for (;;) {
    size_t len = 0;
    uint32_t advance = 0;

    if (!resultSender.headerSent) {
      len = buildStackHeader(buf);
    } else if (resultSender.pixel < PIXEL_COUNT) {
      advance = PIXEL_COUNT - resultSender.pixel;
      if (advance > STACK_PIXELS_PER_CHUNK) advance = STACK_PIXELS_PER_CHUNK;
      len = buildStackPixels(buf, resultSender.pixel, advance);
    } else {
      putU32(buf, resultSender.checksum);
      if (txFree() < 4) return;
      txAppend(buf, 4);
      endResultSend();
      pumpTx();
      return;
    }

    if (txFree() < len) {
      return;
    }
    for (size_t i = 0; i < len; ++i) {
      resultSender.checksum += buf[i];
    }
    txAppend(buf, len);
    if (!resultSender.headerSent) {
      resultSender.headerSent = true;
    } else {
      resultSender.pixel += advance;
    }
    pumpTx();
  }
}

// ===========================================================================
// Frame
// ===========================================================================

// Sends START, runs the scan, and stages the frame for transmission.
void runFrame(uint32_t frameStartTimeUs) {
  const uint32_t lateUs = (uint32_t)(frameStartTimeUs - g_nextFrameStartUs);
  const uint32_t entryUs = micros();

  // Send START again if the CPLD ignores it. That occurs approximately one
  // time in each few hundred frames. A second pulse is safe, because the CPLD
  // did nothing with the first pulse and is therefore idle.
  //
  // Clear the FIFOs inside this loop. The CPLD cannot know that firmware
  // stopped. It continues to send triggers for the rest of an abandoned scan,
  // and those triggers would become the first pixels of the next frame.
  ScanResult result;
  for (uint32_t attempt = 0;; ++attempt) {
    pio_sm_set_enabled(g_pixelTrigPio, g_pixelTrigSm, false);
    drainFilteredTriggers();
    reportFilteredTriggerCount();

    // Send it now, not during the scan.
    //
    // That line is queued about 30 us before START, and loop() cannot drain
    // it because we are already inside runFrame(). So it sat in the queue
    // until the first pumpTx() of the capture loop, which runs immediately
    // after pixel 0. One ~110 byte Serial.write landing there is enough to
    // push that loop pass past the 20 us pixel period, which queues a second
    // trigger, which costs a pixel.
    //
    // Measured before this: staleat was 1 on 34 of 47 stale events, with the
    // rest scattered over indices 11 to 46 and nothing above 46 - the USB IN
    // interrupts carrying the same packet.
    //
    // There is ~46 ms of idle between frames. Bounded so a stalled host
    // cannot hold up the frame.
    const uint32_t flushStartUs = micros();
    while (txPending() > 0 && (uint32_t)(micros() - flushStartUs) < 5000) {
      pumpTx();
    }

    // Then wait for the HOST to collect it, which is a separate event.
    //
    // pumpTx() only moves bytes into the TinyUSB FIFO. The Pico is a
    // full-speed device, so the host schedules bulk IN transfers on 1 ms
    // frame boundaries, and the completion interrupt for that packet was
    // landing inside the scan. Measured with the flush alone: staleat spread
    // uniformly over 3 to 50 and never outside it. 50 pixels at 20 us is
    // 1000 us - one USB frame, which is the signature of exactly that.
    //
    // availableForWrite() runs tud_task(), so polling it also drives the
    // transfer forward. It returns to the full FIFO size only once the host
    // has read, so the interrupt is behind us when this loop exits.
    //
    // Bounded: a host that stops reading delays the frame by 5 ms and shows
    // up in "late" rather than hanging the loop.
    while (Serial.availableForWrite() < g_txFifoCapacity &&
           (uint32_t)(micros() - flushStartUs) < 5000) {
    }

    pio_sm_clear_fifos(g_pixelTrigPio, g_pixelTrigSm);
    pio_sm_clear_fifos(g_pixelTrigPio, g_rawEdgeSm);
    filteredTriggerCount.frameSequence = frameSequence;
    filteredTriggerCount.eventCount = 0;
    filteredTriggerCount.active = true;
    filteredTriggerCount.firstExtraGapUs = 0;
    filteredTriggerCount.minExtraGapUs = UINT32_MAX;
    filteredTriggerCount.maxExtraGapUs = 0;
    pio_sm_set_enabled(g_pixelTrigPio, g_pixelTrigSm, true);

    digitalWrite(PIN_START, HIGH);
    delayMicroseconds(START_PULSE_WIDTH_US);
    digitalWrite(PIN_START, LOW);

    result = runScan(pixelBuffer);
    result.startRetries = attempt;

    if (!result.neverStarted || attempt >= START_RETRY_LIMIT) {
      break;
    }
  }

  // Advance from the SCHEDULED time, not from the current time. Error then
  // cannot accumulate.
  g_nextFrameStartUs = frameStartTimeUs + g_framePeriodUs;

  const uint32_t doneFrameSeq = frameSequence;
  ++frameSequence;

  // A capture consumes the frame instead of transmitting it. QUIET keeps
  // scanning but stages nothing. Both leave the link idle, which is the
  // point: the USB interrupts a transmission produces are what cost pixels
  // during a scan. See the flush at the top of this function.
  if (g_mode == MODE_CAPTURE) {
    captureOnFrame(result, lateUs);
    pumpTx();
    return;
  }
  if (g_mode == MODE_QUIET) {
    pumpTx();
    return;
  }

  const uint16_t code = (REPORT_PIXEL_INDEX <= PIXEL_COUNT)
                            ? pixelBuffer[REPORT_PIXEL_INDEX - 1]
                            : MISSING_PIXEL;
  const uint32_t millivolts =
      (code == MISSING_PIXEL) ? 0 : ((uint32_t)code * ADC_VREF_MV) / ADC_FULL_SCALE;

  const uint32_t durationUs = (uint32_t)(micros() - entryUs);

  // Stage the frame and the status line as one message. A reply then cannot
  // divide them. Stage them only if the previous frame is fully sent. If it
  // is not, drop this frame instead. The host shows the newest complete
  // frame, so a dropped frame is a missed update, not corrupt data.
  //
  // txFrameReady() also refuses while a record is going out. See its comment:
  // a result transfer never met that, because a capture leaves the device in
  // QUIET and the two returns above take the frame away before it gets here,
  // but reading a file off the card does.
  static char frameBuf[FRAME_BINARY_BYTES + 448];
  if (txFrameReady(sizeof(frameBuf))) {
    size_t off = stageBinaryFrame(frameBuf, doneFrameSeq, result.capturedCount,
                                  pixelBuffer);
    if (g_txVerbosity >= 1) {
      off += snprintf(
          frameBuf + off, sizeof(frameBuf) - off,
          "S %-6lu pulses %4lu raw %4lu miss %4lu period %2luus wdog %1d "
          "nostart %1d retry %1lu early %4lu fifo %1lu gap %3lu-%4luus "
          "late %4luus dur %6luus maxread %2luus txdrop %4lu "
          "stale %4lu badbits %3lu px%lu %5u (0x%04X, %4lu mV)",
          (unsigned long)doneFrameSeq, (unsigned long)result.capturedCount,
          (unsigned long)result.rawEdgeCount, (unsigned long)result.missingCount,
          (unsigned long)g_pixelPeriodUs, (int)result.timedOut,
          (int)result.neverStarted, (unsigned long)result.startRetries,
          (unsigned long)result.earlyDeassertEdges,
          (unsigned long)result.maxFifoBacklog,
          (unsigned long)(result.minRawGapUs == UINT32_MAX ? 0 : result.minRawGapUs),
          (unsigned long)result.maxRawGapUs, (unsigned long)lateUs,
          (unsigned long)durationUs, (unsigned long)result.maxReadDurationUs,
          (unsigned long)g_txDroppedFrames,
          (unsigned long)result.staleTriggers,
          (unsigned long)result.badBitsDropped,
          (unsigned long)REPORT_PIXEL_INDEX,
          (unsigned)code, (unsigned)code, (unsigned long)millivolts);
    }

    // Fault-hunting fields. Not part of the documented line; they exist to
    // chase whatever is current and can be dropped when it is resolved.
    if (g_txVerbosity >= 2) {
      off += snprintf(
          frameBuf + off, sizeof(frameBuf) - off,
          " staleat %4lu reread %3lu badat %4lu/%4lu/%4lu/%4lu badus %10lu "
          "bad %04X/%04X/%04X lowbits %4lu/%4lu/%4lu/%4lu "
          "raw16 0x%04X sclk %4lu rdelay %2lu",
          (unsigned long)result.firstStaleIndex,
          (unsigned long)result.rereadPixels,
          (unsigned long)result.badIndex[0], (unsigned long)result.badIndex[1],
          (unsigned long)result.badIndex[2], (unsigned long)result.badIndex[3],
          (unsigned long)result.firstBadUs,
          (unsigned)result.firstBadWords[0], (unsigned)result.firstBadWords[1],
          (unsigned)result.firstBadWords[2],
          (unsigned long)result.lowBitsHist[0],
          (unsigned long)result.lowBitsHist[1],
          (unsigned long)result.lowBitsHist[2],
          (unsigned long)result.lowBitsHist[3], (unsigned)g_lastRawWord,
          (unsigned long)g_sclkDelayNops, (unsigned long)g_readDelayUs);
    }

    if (g_txVerbosity >= 1) {
      off += snprintf(frameBuf + off, sizeof(frameBuf) - off, "\n");
    }
    txAppend(frameBuf, off);
  } else {
    ++g_txDroppedFrames;
  }
  pumpTx();
}

// ===========================================================================
// Wavelength calibration store
//
// The device holds the calibration so that it belongs to the INSTRUMENT and
// not to whichever computer took the last measurement. It lives in the
// RP2350's own flash rather than on the card, because a card can be swapped
// or absent, and a pixel-to-wavelength map does not travel with the data. It
// travels with the optics.
//
// What is stored is the REFERENCE POINTS, not the fitted polynomial. The
// polynomial follows from the points and from the degree rule, and that rule
// lives in exactly one place: calibration.py on the host. Storing the
// coefficients as well would let the two disagree, with nothing to say which
// was right.
//
// The text is opaque here. Firmware checks the character set and the length,
// then stores the bytes. It never parses a number out of them. Nothing on
// the device uses a wavelength.
//
// Format: "<pixel>:<nm>,<pixel>:<nm>,..." for example
//   12.0:435.833,300.5:546.074,880.2:696.543
// ===========================================================================

constexpr char CAL_PATH[] = "/cal.txt";

// This sets the command line length. See pollHostCommands(): the buffer has
// to hold "cal set " and this.
//
// Sized from the worst case, not the typical one. The host writes a point as
// "<pixel>:<nm>," and the widest that can be is "1023.99:1099.9990," at 18
// characters. 16 points is therefore 288, and 320 leaves margin at no cost:
// the buffer is static and this is a 2 MB part.
constexpr size_t CAL_TEXT_MAX = 320;

struct CalStore {
  bool fsReady = false;   // the flash filesystem mounted
  uint32_t points = 0;    // colons in the text, one per point
  char text[CAL_TEXT_MAX] = "";
};

CalStore g_cal;

static uint32_t calCountPoints(const char *text) {
  uint32_t n = 0;
  for (const char *p = text; *p != '\0'; ++p) {
    if (*p == ':') ++n;
  }
  return n;
}

// A whitelist, for the same reason sdValidName() is a whitelist: this string
// arrives from the host and is written to a file.
static bool calValidText(const char *text) {
  const size_t n = strlen(text);
  if (n == 0 || n >= CAL_TEXT_MAX) {
    return false;
  }
  for (size_t i = 0; i < n; ++i) {
    const char c = text[i];
    const bool ok = (c >= '0' && c <= '9') || c == '.' || c == ':' ||
                    c == ',' || c == '-' || c == '+' || c == 'e' || c == 'E';
    if (!ok) {
      return false;
    }
  }
  return calCountPoints(text) >= 2;  // a line needs two points
}

// Reads the stored calibration into RAM. Called once, from setup().
void calLoad() {
  g_cal.text[0] = '\0';
  g_cal.points = 0;
  if (!g_cal.fsReady) {
    return;
  }
  auto f = LittleFS.open(CAL_PATH, "r");
  if (!f) {
    return;
  }
  const size_t n = (size_t)f.size();
  if (n > 0 && n < CAL_TEXT_MAX) {
    const size_t got = f.readBytes(g_cal.text, n);
    g_cal.text[got] = '\0';
    // The file ends in a newline. Stop at the first one.
    for (size_t i = 0; i < got; ++i) {
      if (g_cal.text[i] == '\n' || g_cal.text[i] == '\r') {
        g_cal.text[i] = '\0';
        break;
      }
    }
  }
  f.close();
  // A damaged file reads as no calibration rather than as a bad one. A wrong
  // wavelength axis is not visibly wrong, so it must not be possible to get
  // one from a file this code could not read.
  if (!calValidText(g_cal.text)) {
    g_cal.text[0] = '\0';
  }
  g_cal.points = calCountPoints(g_cal.text);
}

// Writes RAM to flash. The caller reports a failure: a calibration the host
// believes was saved and was not is worse than one it knows to send again.
bool calSave() {
  if (!g_cal.fsReady) {
    return false;
  }
  if (g_cal.text[0] == '\0') {
    LittleFS.remove(CAL_PATH);
    return true;
  }
  auto f = LittleFS.open(CAL_PATH, "w");
  if (!f) {
    return false;
  }
  const size_t want = strlen(g_cal.text);
  const size_t wrote = f.write((const uint8_t *)g_cal.text, want);
  f.write((uint8_t)'\n');
  f.close();
  return (wrote == want);
}

void emitCalibration() {
  char line[CAL_TEXT_MAX + 64];
  snprintf(line, sizeof(line), "K cal n=%lu bytes=%u store=%d points=%s",
           (unsigned long)g_cal.points, (unsigned)strlen(g_cal.text),
           (int)g_cal.fsReady, g_cal.text[0] ? g_cal.text : "none");
  txLine(line);
}

// "cal ..." holds the wavelength calibration. Returns true if this function
// handled cmd.
//
//   cal, cal get, cal status     report it
//   cal set <p:nm,p:nm,...>      replace it and write flash
//   cal clear                    erase it and the file
bool handleCalCommand(const char *cmd) {
  if (strncmp(cmd, "cal", 3) != 0 || (cmd[3] != ' ' && cmd[3] != '\0')) {
    return false;
  }
  const char *rest = (cmd[3] == '\0') ? "get" : cmd + 4;
  while (*rest == ' ') ++rest;

  if (rest[0] == '\0' || strcmp(rest, "get") == 0 ||
      strcmp(rest, "status") == 0) {
    emitCalibration();
    return true;
  }

  if (strcmp(rest, "clear") == 0) {
    g_cal.text[0] = '\0';
    g_cal.points = 0;
    if (!calSave()) {
      txLine("E cal store_failed");
      return true;
    }
    emitCalibration();
    return true;
  }

  if (strncmp(rest, "set ", 4) == 0) {
    const char *arg = rest + 4;
    while (*arg == ' ') ++arg;
    if (!calValidText(arg)) {
      txLine("E cal bad_points");
      return true;
    }
    // Into RAM first, then to flash. A write that fails leaves the device
    // using what the host just sent, and says so, rather than silently
    // reverting to the old map.
    snprintf(g_cal.text, sizeof(g_cal.text), "%s", arg);
    g_cal.points = calCountPoints(g_cal.text);
    if (!calSave()) {
      txLine("E cal store_failed");
      return true;
    }
    emitCalibration();
    return true;
  }

  txLine("E cal unknown_subcommand");
  return true;
}

// ===========================================================================
// Host command interface
//
// The device uses plain ASCII requests and replies on the frame stream port.
// The host application reads the "I " line to identify the device and to get
// the acquisition parameters. That format is a contract. You can add a
// key=value field. Do not rename or reorder an existing field.
// ===========================================================================

constexpr char FW_VERSION[] = "1.4.0";
constexpr char FW_BOARD[] = "rpipico2";

void emitDeviceInfo() {
  uint32_t pioChecksum = 0;
  for (uint8_t i = 0; i < pixel_trig_program.length; ++i) {
    pioChecksum = (pioChecksum * 31u) + pixel_trig_program_instructions[i];
  }

  // A value in this line must not contain a space. __DATE__ contains two.
  char buildDate[16];
  snprintf(buildDate, sizeof(buildDate), "%s", __DATE__);
  for (char *p = buildDate; *p; ++p) {
    if (*p == ' ') *p = '_';
  }

  char line[560];
  snprintf(line, sizeof(line),
           "I fw=%s proto=3 board=%s pixels=%lu bits=14 vref_mv=%lu "
           "rate_hz=%lu rate_min=%lu rate_max=%lu pixel_period_us=%lu "
           "frame_us=%lu frame_us_min=%lu frame_us_max=%lu "
           "refractory_us=%lu sysclk_mhz=%lu pio_crc=0x%08lX "
           "missing_code=0x%04X stack=1 stack_block=%lu stack_block_max=%lu "
           "stack_sigma_x100=%lu cap_max_frames=%lu cap_max_s=%lu mode=%d "
           "sd=1 cal=1 cal_n=%lu cal_store=%d selftest=%d built=%s",
           FW_VERSION, FW_BOARD, (unsigned long)PIXEL_COUNT,
           (unsigned long)ADC_VREF_MV, (unsigned long)g_frameRateHz,
           (unsigned long)FRAME_RATE_MIN_HZ, (unsigned long)FRAME_RATE_MAX_HZ,
           (unsigned long)g_pixelPeriodUs, (unsigned long)g_framePeriodUs,
           (unsigned long)FRAME_PERIOD_MIN_US, (unsigned long)FRAME_PERIOD_MAX_US,
           (unsigned long)REFRACTORY_US,
           (unsigned long)(clock_get_hz(clk_sys) / 1000000u),
           (unsigned long)pioChecksum, (unsigned)MISSING_PIXEL,
           (unsigned long)STACK_BLOCK_FRAMES_DEFAULT,
           (unsigned long)STACK_BLOCK_FRAMES_MAX,
           (unsigned long)STACK_SIGMA_X100_DEFAULT,
           (unsigned long)CAPTURE_MAX_FRAMES, (unsigned long)CAPTURE_MAX_SECONDS,
           (int)g_mode, (unsigned long)g_cal.points, (int)g_cal.fsReady,
           (int)g_selfTestPass, buildDate);
  txLine(line);
}

// Hardware test commands. None of them runs unless you send it. Returns true
// if this function handled cmd.
bool handleDiagnosticCommand(const char *cmd) {
  // Reads the CFR several times. This is the only test of the SDI direction,
  // because a pixel read tests SDO only. A stable ADC_CFR_DEFAULT value
  // proves that SDI works.
  if (strcmp(cmd, "cfr") == 0) {
    char line[160];
    int off = snprintf(line, sizeof(line), "H cfr");
    uint32_t matches = 0;
    for (int i = 0; i < 8; ++i) {
      const uint16_t value = adcCommand(ADC_CMD_READ_CFR) & ADC_CFR_MASK;
      // Mask the unimplemented bits. See ADC_CFR_IGNORED.
      if ((value & ~ADC_CFR_IGNORED) == ADC_CFR_DEFAULT) ++matches;
      off += snprintf(line + off, sizeof(line) - off, " %03X", (unsigned)value);
    }
    snprintf(line + off, sizeof(line) - off, " match=%lu/8 (expect %03X)",
             (unsigned long)matches, (unsigned)ADC_CFR_DEFAULT);
    txLine(line);
    return true;
  }

  // Writes a CFR value, then reads it back immediately. The order is
  // important. A pixel read between the two would load the defaults and hide
  // the result. The value does not remain after the next pixel.
  if (strncmp(cmd, "cfrw ", 5) == 0) {
    const uint16_t value = (uint16_t)(strtoul(cmd + 5, nullptr, 16) & ADC_CFR_MASK);
    adcCommand(ADC_CMD_WRITE_CFR | value);
    const uint16_t readback = adcCommand(ADC_CMD_READ_CFR) & ADC_CFR_MASK;
    char line[96];
    snprintf(line, sizeof(line), "H cfrw wrote=%03X readback=%03X %s",
             (unsigned)value, (unsigned)readback,
             (value == readback) ? "MATCH" : "MISMATCH");
    txLine(line);
    return true;
  }

  // The SPI self check on its own. See adcSpiSelfTest() for what it proves
  // and why a pixel read cannot prove it.
  if (strcmp(cmd, "sditest") == 0) {
    if (g_mode == MODE_CAPTURE) {
      txLine("E sditest busy_capturing");
      return true;
    }
    emitSpiSelfTest(adcSpiSelfTest());
    return true;
  }

  // The SPI self check and the ADC reset together: the same sequence the
  // device runs at boot. See runSelfTest().
  if (strcmp(cmd, "selftest") == 0) {
    if (g_mode == MODE_CAPTURE) {
      txLine("E selftest busy_capturing");
      return true;
    }
    (void)runSelfTest();
    return true;
  }

  // Repeats the read-CFR transfer for a few seconds, so a free-running scope
  // catches SDI without a trigger.
  //
  // One "cfr" is 8 transfers of about 16 bit times each. That whole burst
  // lasts tens of microseconds and happens once. A scope at any timebase that
  // shows seconds displays it as an unchanging high level, which is why SDI
  // looks dead when it is working.
  //
  // What to expect on SDI during this command. The read command is C000h, so
  // the 16 bits sent are 1,1,0,0 then twelve 0s. SDI therefore sits HIGH for
  // the first two bit times and LOW for the remaining fourteen. adcCommand()
  // returns SDI to high between transfers. So SDI is mostly LOW during the
  // burst, not high.
  //
  // Probe SDI and CS together. CS frames each transfer.
  if (strncmp(cmd, "cfrloop ", 8) == 0) {
    if (g_mode == MODE_CAPTURE) {
      txLine("E cfrloop busy_capturing");
      return true;
    }
    long secs = atol(cmd + 8);
    if (secs < 1) secs = 1;
    if (secs > 10) secs = 10;

    const uint32_t startMs = millis();
    const uint32_t durationMs = (uint32_t)secs * 1000u;
    uint32_t transfers = 0;
    uint32_t matches = 0;
    uint16_t last = 0;

    // The inner run keeps the transfers close together. Reading millis() on
    // every transfer would stretch the gap between them and lower the rate
    // the scope sees.
    while ((millis() - startMs) < durationMs) {
      for (int i = 0; i < 200; ++i) {
        last = adcCommand(ADC_CMD_READ_CFR) & ADC_CFR_MASK;
        if ((last & ~ADC_CFR_IGNORED) == ADC_CFR_DEFAULT) ++matches;
        ++transfers;
      }
    }

    char line[128];
    snprintf(line, sizeof(line),
             "H cfrloop secs=%ld transfers=%lu match=%lu last=%03X expect=%03X",
             secs, (unsigned long)transfers, (unsigned long)matches,
             (unsigned)last, (unsigned)ADC_CFR_DEFAULT);
    txLine(line);
    return true;
  }

  // Sets the SCLK half period. A value of 0 gives the normal fast clock. A
  // large value exceeds the per-pixel budget and drops pixels.
  if (strncmp(cmd, "sclk ", 5) == 0) {
    long nops = atol(cmd + 5);
    if (nops < 0) nops = 0;
    if (nops > 1000) nops = 1000;
    g_sclkDelayNops = (uint32_t)nops;
    char line[64];
    snprintf(line, sizeof(line), "A sclk_nops=%lu", (unsigned long)g_sclkDelayNops);
    txLine(line);
    return true;
  }

  // Sends the last read triples that did not agree, as raw 16-bit words.
  // Sets a settling delay before each read. If the first read catches an
  // unfinished conversion, this delay reduces the nomajority count.
  // Controls how much text accompanies each frame. See TX_VERBOSITY_DEFAULT.
  if (strncmp(cmd, "verbose ", 8) == 0) {
    const unsigned long level = strtoul(cmd + 8, nullptr, 10);
    g_txVerbosity = (level > 2) ? 2 : (uint32_t)level;
    char line[64];
    snprintf(line, sizeof(line), "A verbose=%lu", (unsigned long)g_txVerbosity);
    txLine(line);
    return true;
  }

  if (strncmp(cmd, "readdelay ", 10) == 0) {
    long us = atol(cmd + 10);
    if (us < 0) us = 0;
    if (us > 10) us = 10;
    g_readDelayUs = (uint32_t)us;
    char line[64];
    snprintf(line, sizeof(line), "A readdelay_us=%lu", (unsigned long)g_readDelayUs);
    txLine(line);
    return true;
  }

  // Resets the ADC and reports whether the reset actually took effect. See
  // adcSoftwareResetVerified() for how the proof works.
  //
  // "restored" is the check that matters. It is meaningful only when "armed"
  // passed: if the CFR never moved off its default, finding it at the default
  // afterwards says nothing, and the reply says so rather than claiming a
  // pass.
  //
  // "postreset_sdo" is a second, independent signature: SDO carries a
  // saturated result for the one cycle after a reset. See
  // ADC_POST_RESET_WORD for why that word is FFFC and not FFFF.
  if (strcmp(cmd, "adcreset") == 0) {
    if (g_mode == MODE_CAPTURE) {
      txLine("E adcreset busy_capturing");
      return true;
    }
    emitAdcReset(adcSoftwareResetVerified());
    return true;
  }

  // Characterizes the ADC pins without a scope. Reports two tests per pin.
  //
  // DRIVE TEST. The firmware drives each level, then reads the pin back
  // through the input synchronizer. A working pad on a free net reads back
  // what it drives. A mismatch means something outside the RP2350 holds the
  // net.
  //
  // PULL TEST. The firmware releases the pin, then applies each internal
  // pull and reads the result. The internal pulls are weak, roughly 50-80
  // kOhm, so any real driver or any board resistor below about 5 kOhm
  // overrides them. This characterizes the net when the RP2350 does not
  // drive it.
  //
  // How to read the result:
  //
  //   drive=1/0  pull=1/0   pad works, net is free.        Suspect the far end.
  //   drive=1/0  pull=1/1   pad works, board holds high.   Pullup on the net.
  //   drive=1/0  pull=0/0   pad works, board holds low.    Pulldown on the net.
  //   drive=0/0             net is stuck low.  Short to ground, or a driver.
  //   drive=1/1             net is stuck high. Short to VDD, or a driver.
  //
  // A stuck net that ignores the drive test is the case that matters here. It
  // is not a firmware fault, and moving to another GPIO will not repair the
  // connection to the ADC.
  //
  // MISO is an input from the ADC. Driving it would put two outputs on one
  // net, so this reports the pull test for that pin alone.
  if (strcmp(cmd, "pindiag") == 0) {
    if (g_mode == MODE_CAPTURE) {
      txLine("E pindiag busy_capturing");
      return true;
    }

    struct PinUnderTest {
      uint8_t gpio;
      const char *name;
      bool drivable;   // false for an ADC output, to avoid bus contention
    };
    const PinUnderTest pins[] = {
        {PIN_ADC_MOSI, "mosi", true},
        {PIN_ADC_SCLK, "sclk", true},
        {PIN_ADC_CS, "cs", true},
        {PIN_ADC_MISO, "miso", false},
    };

    for (const PinUnderTest &put : pins) {
      int driveHigh = -1;
      int driveLow = -1;

      if (put.drivable) {
        pinMode(put.gpio, OUTPUT);
        digitalWrite(put.gpio, HIGH);
        delayMicroseconds(50);           // settles the net and the synchronizer
        driveHigh = digitalRead(put.gpio) ? 1 : 0;
        digitalWrite(put.gpio, LOW);
        delayMicroseconds(50);
        driveLow = digitalRead(put.gpio) ? 1 : 0;
      }

      // The weak pulls charge the net slowly, so these waits are much longer
      // than the drive waits above.
      pinMode(put.gpio, INPUT_PULLUP);
      delay(2);
      const int pullUp = digitalRead(put.gpio) ? 1 : 0;
      pinMode(put.gpio, INPUT_PULLDOWN);
      delay(2);
      const int pullDown = digitalRead(put.gpio) ? 1 : 0;

      const char *verdict;
      if (put.drivable && driveHigh == 0 && driveLow == 0) {
        verdict = "STUCK_LOW";
      } else if (put.drivable && driveHigh == 1 && driveLow == 1) {
        verdict = "STUCK_HIGH";
      } else if (pullUp == 1 && pullDown == 0) {
        verdict = "ok_net_free";
      } else if (pullUp == 1 && pullDown == 1) {
        verdict = "ok_net_held_high";
      } else {
        verdict = "ok_net_held_low";
      }

      char line[112];
      if (put.drivable) {
        snprintf(line, sizeof(line),
                 "H pindiag %s gpio=%u drive1=%d drive0=%d pu=%d pd=%d %s",
                 put.name, (unsigned)put.gpio, driveHigh, driveLow, pullUp,
                 pullDown, verdict);
      } else {
        snprintf(line, sizeof(line),
                 "H pindiag %s gpio=%u drive=skipped_input pu=%d pd=%d %s",
                 put.name, (unsigned)put.gpio, pullUp, pullDown, verdict);
      }
      txLine(line);
    }

    // Restores the idle state that setup() established. Without this the ADC
    // pins stay as inputs and every later read returns nonsense.
    pinMode(PIN_ADC_CS, OUTPUT);
    digitalWrite(PIN_ADC_CS, HIGH);
    pinMode(PIN_ADC_MISO, INPUT);
    pinMode(PIN_ADC_MOSI, OUTPUT);
    digitalWrite(PIN_ADC_MOSI, HIGH);
    pinMode(PIN_ADC_SCLK, OUTPUT);
    digitalWrite(PIN_ADC_SCLK, LOW);
    txLine("H pindiag=done pins_restored");
    return true;
  }

  return false;
}

// Finds "key=" in cmd, at the start of the string or after a space, and
// returns the text after the equals sign. Returns nullptr if the key is
// absent.
//
// The position test matters: without it "exp=" would also match inside
// "myexp=", and a mistyped key would silently set a real parameter.
const char *findArg(const char *cmd, const char *key) {
  const size_t klen = strlen(key);
  for (const char *p = cmd; *p != '\0'; ++p) {
    if (p != cmd && p[-1] != ' ') {
      continue;
    }
    if (strncmp(p, key, klen) == 0 && p[klen] == '=') {
      return p + klen + 1;
    }
  }
  return nullptr;
}

bool argU32(const char *cmd, const char *key, uint32_t *out) {
  const char *v = findArg(cmd, key);
  if (v == nullptr) {
    return false;
  }
  *out = (uint32_t)strtoul(v, nullptr, 10);
  return true;
}

bool argFloat(const char *cmd, const char *key, float *out) {
  const char *v = findArg(cmd, key);
  if (v == nullptr) {
    return false;
  }
  *out = (float)atof(v);
  return true;
}

static inline uint32_t clampU32(uint32_t v, uint32_t lo, uint32_t hi) {
  if (v < lo) return lo;
  if (v > hi) return hi;
  return v;
}

// ===========================================================================
// SD card storage
//
// Every stacked capture is written to the card as a file, and the same file
// can be listed, sent back to the host, or deleted on request.
//
// The file holds the stack record BYTE FOR BYTE as "cap get" transmits it:
// the same magic, the same 64 byte header, the same 12 bytes per pixel and
// the same trailing checksum. Nothing is re-encoded. That has three
// consequences worth stating, because they are the reason for the choice.
//
//   The host needs no second parser. A file sent back by "sd get" arrives on
//   the same stream and is decoded by the same code that decodes a live
//   result, so a stored spectrum and a fresh one cannot drift apart.
//
//   The checksum inside the record covers the file. A card that returns a
//   corrupted sector fails the same test a corrupted transfer fails.
//
//   The header is self-describing. The listing reads it back out of each
//   file, so the card carries its own catalogue and there is no index file
//   to fall out of step with the directory.
//
// Files live in /SPECTRA with 8.3 names, TTNNNNNN.STK: a two letter tag from
// the host, then a six digit sequence number. The tag is what the host calls
// the capture (SP spectrum, DK dark, FL flat); the firmware only stores it.
// The sequence number continues from the highest already on the card, which
// is found by reading the directory at mount. A card moved between two
// instruments therefore cannot overwrite its own files.
//
// The RP2350 has no real-time clock. "sd time <epoch>" hands the firmware the
// host clock, and the FAT timestamp callback below uses it, so files carry
// real dates that a card reader also shows. Without it the dates read as
// 2000-01-01, which is wrong but harmless.
// ===========================================================================

// ---------------------------------------------------------------------------
// Card file container and codec
//
// A stored file is the stack record, optionally compressed. The record itself
// is unchanged: the same fields, the same order, the same trailing checksum.
// Compression is a property of the FILE, not of the data, so "sd get" still
// puts a plain record on the wire and the host decoder never learns that any
// of this exists.
//
// Layout, little-endian throughout:
//
//   uint8[4]  magic  A5 5A 5D 01   (not the frame magic, not the stack magic)
//   uint16    container version
//   uint16    codec, 0 none, 1 shuffle+rle
//   uint32    plain bytes, the size of the record this file holds
//   uint32    stored bytes, the length of the payload region below
//   uint16    stride, the width of one pixel record
//   uint16    record header bytes
//   uint32    sum of the payload bytes
//                                        (24 bytes to here)
//   uint8[64] the record header, VERBATIM, never compressed
//   uint8[]   the payload: the pixel bytes, through the codec
//   uint8[4]  the record checksum, verbatim
//
// The record header stays plain so that a listing can read the capture id,
// the exposure and the frame counts straight out of every file without
// decompressing any of them. It is 64 bytes; compressing it would save about
// thirty and cost the listing its speed.
//
// WHY SHUFFLE. The pixel array is 1024 records of 12 bytes, and a byte's
// compressibility depends entirely on which of the twelve it is. The top
// byte of a float32 mean is nearly constant across a spectrum, the accepted
// count is nearly constant, and the low mantissa bytes are noise. Interleaved
// as they are on the wire, every run is one byte long and run-length coding
// achieves nothing at all: measured 12607 bytes out for 12288 in, i.e. it
// makes the file BIGGER. Transposed so that each byte position becomes a
// contiguous plane, the same coder reaches about 1.6 to 1 on real spectra.
//
// WHY NOT MORE. Measured against a small-window LZ with Huffman coding, the
// best a general compressor manages on this record is about 1.9 to 1, and an
// entirely different record format storing the integer accumulators would
// reach about 2.5. The reason all three are close is that roughly half the
// record is float32 mantissa in mean and sd, which is measurement noise and
// therefore incompressible by anything lossless. 1.6 with no dependency and
// no heap was judged the better trade. Window size, incidentally, is
// irrelevant here: a 2 KB window scores the same as 32 KB, because the win
// comes from the near-constant planes and not from long matches.
// ---------------------------------------------------------------------------

constexpr uint8_t SD_FILE_MAGIC[4] = {0xA5, 0x5A, 0x5D, 0x01};
constexpr uint16_t SD_FILE_VERSION = 1;
constexpr size_t SD_FILE_HEADER_BYTES = 24;

constexpr uint16_t SD_CODEC_NONE = 0;
constexpr uint16_t SD_CODEC_RLE = 1;   // byte-plane shuffle, then run-length

// The pixel region, which is the only part that goes through the codec.
constexpr size_t SD_PIXEL_BYTES = STACK_PIXEL_BYTES * PIXEL_COUNT;

// Worst case output of the coder: no run anywhere, so every byte arrives as a
// literal and each block of 255 costs two bytes of framing. Sized to that, not
// to the expected output, because an encoder that can overrun its buffer on
// incompressible input is an encoder that fails exactly when the data is
// unusual.
constexpr size_t SD_PAYLOAD_MAX = SD_PIXEL_BYTES + (SD_PIXEL_BYTES / 255) * 2 + 8;

// Writes the 24 byte container header. Returns its length.
//
// Kept here, next to the layout comment and inside the block the tests
// extract, so that the writer and the readers cannot drift apart. There are
// three readers: sdLoadRecord() below, sdEmitFileLine() for the listing, and
// tools/read_stk.py for a card in a card reader.
size_t sdBuildFileHeader(uint8_t *out, uint16_t codec, uint32_t payloadBytes,
                         uint32_t payloadSum) {
  uint8_t *p = out;
  memcpy(p, SD_FILE_MAGIC, sizeof(SD_FILE_MAGIC));
  p += sizeof(SD_FILE_MAGIC);
  p = putU16(p, SD_FILE_VERSION);
  p = putU16(p, codec);
  p = putU32(p, (uint32_t)STACK_RECORD_BYTES);
  p = putU32(p, payloadBytes);
  p = putU16(p, (uint16_t)STACK_PIXEL_BYTES);
  p = putU16(p, (uint16_t)STACK_HEADER_BYTES);
  p = putU32(p, payloadSum);
  return (size_t)(p - out);
}

// Byte j of the transposed pixel array.
//
// Plane k holds byte k of every pixel record, so the transposed stream is
// plane 0 in full, then plane 1, and so on. This is the whole of the shuffle;
// there is no buffer, because the coder reads the array through here instead
// of transposing it first. That saves 12 KB of RAM and one copy.
static inline uint8_t sdShuffledByte(const uint8_t *pixels, uint32_t count,
                                     uint32_t stride, uint32_t j) {
  const uint32_t index = j % count;
  const uint32_t plane = j / count;
  return pixels[index * stride + plane];
}

// Run-length coder over the transposed pixel array.
//
//   <n><value>            n from 1 to 255, a run of n copies of value
//   <0><n><n bytes>       n from 1 to 255 literal bytes
//
// A run is only worth emitting at three bytes or more: two costs the same as
// two literals inside an existing literal block, and coding it separately
// would end that block and pay for a new header.
//
// Returns the number of bytes written, or 0 if the output does not fit. Zero
// is not a valid encoding of a non-empty input, so the caller can treat it as
// the failure it is; sdSaveResult() falls back to storing the record plain.
size_t sdEncodePixels(const uint8_t *pixels, uint32_t count, uint32_t stride,
                      uint8_t *out, size_t cap) {
  const uint32_t total = count * stride;
  size_t w = 0;
  uint32_t i = 0;

  while (i < total) {
    const uint8_t first = sdShuffledByte(pixels, count, stride, i);
    uint32_t run = 1;
    while (run < 255 && i + run < total &&
           sdShuffledByte(pixels, count, stride, i + run) == first) {
      ++run;
    }

    if (run >= 3) {
      if (w + 2 > cap) return 0;
      out[w++] = (uint8_t)run;
      out[w++] = first;
      i += run;
      continue;
    }

    // Literals, gathered until a run of three appears or the block is full.
    const uint32_t start = i;
    while (i < total && (i - start) < 255) {
      if (i + 2 < total) {
        const uint8_t a = sdShuffledByte(pixels, count, stride, i);
        const uint8_t b = sdShuffledByte(pixels, count, stride, i + 1);
        const uint8_t c = sdShuffledByte(pixels, count, stride, i + 2);
        if (a == b && b == c) break;
      }
      ++i;
    }
    const uint32_t len = i - start;
    if (w + 2 + len > cap) return 0;
    out[w++] = 0;
    out[w++] = (uint8_t)len;
    for (uint32_t k = 0; k < len; ++k) {
      out[w++] = sdShuffledByte(pixels, count, stride, start + k);
    }
  }
  return w;
}

// The inverse. Writes straight into the pixel array in its wire order, so the
// transpose is undone by the same index arithmetic that applied it.
//
// Every write is bounds-checked against the pixel array and every read
// against the input. This decoder is fed bytes off a memory card, which is to
// say bytes that may be damaged, and a length field that has flipped a bit
// must end the decode rather than run off the end of the buffer.
//
// Returns the number of pixel bytes produced, or 0 on any malformed input.
size_t sdDecodePixels(const uint8_t *in, size_t n, uint8_t *pixels,
                      uint32_t count, uint32_t stride) {
  const uint32_t total = count * stride;
  uint32_t j = 0;
  size_t r = 0;

  while (r < n) {
    const uint8_t token = in[r++];
    if (token == 0) {
      if (r >= n) return 0;
      const uint32_t len = in[r++];
      if (len == 0 || r + len > n || j + len > total) return 0;
      for (uint32_t k = 0; k < len; ++k) {
        pixels[(j % count) * stride + (j / count)] = in[r + k];
        ++j;
      }
      r += len;
    } else {
      if (r >= n) return 0;
      const uint8_t value = in[r++];
      if (j + token > total) return 0;
      for (uint32_t k = 0; k < token; ++k) {
        pixels[(j % count) * stride + (j / count)] = value;
        ++j;
      }
    }
  }
  return (j == total) ? (size_t)total : 0;
}

constexpr char SD_DIR[] = "/SPECTRA";
constexpr char SD_EXT[] = ".STK";
constexpr size_t SD_NAME_MAX = 13;  // 8.3 plus the terminator
constexpr uint32_t SD_SEQ_MAX = 999999;

// Clock rates tried at mount, fastest first. See test/../src/sd_test.cpp:
// a card that only answers at a low rate is a wiring fault, not a slow card,
// and the rate that worked is reported so that fault stays visible.
constexpr uint32_t SD_CLOCK_TRY_MHZ[] = {24, 16, 8, 4};

// Pixels per write. The same chunk size the transmitter uses, so both paths
// build the record with one buffer of the same shape.
constexpr uint32_t SD_PIXELS_PER_CHUNK = STACK_PIXELS_PER_CHUNK;

SdFat g_sd;

// One record, assembled whole.
//
// The transmitter builds the record in 384 byte chunks and never holds all of
// it, which is right for the transmitter: the queue is smaller than the
// record. The card path cannot do that. The shuffle reads byte k of every
// pixel in turn, so it needs the whole pixel array addressable, and the read
// path has to decode a file before it can check the record's own checksum.
// One buffer serves both directions and both codecs.
uint8_t g_sdRecord[STACK_RECORD_BYTES];
uint8_t g_sdPayload[SD_PAYLOAD_MAX];

struct SdState {
  bool mounted = false;
  bool autoSave = true;   // write each finished capture without being asked
  bool savePending = false;
  uint16_t codec = SD_CODEC_RLE;  // how new files are written
  uint32_t lastPlainBytes = 0;    // the last file, before and after the codec
  uint32_t lastStoredBytes = 0;
  uint32_t clockMhz = 0;
  uint32_t nextSeq = 1;
  uint32_t filesAtMount = 0;
  uint32_t writes = 0;
  uint32_t writeErrors = 0;
  char pendingTag[3] = "SP";
  char lastFile[SD_NAME_MAX] = "";
  char lastError[48] = "";
};

SdState g_sdState;

// Host clock, as a Unix epoch second, and the micros() reading when it was
// set. Zero means no clock has been supplied.
uint32_t g_sdEpochRef = 0;
uint32_t g_sdEpochSetUs = 0;

static uint32_t sdNowEpoch() {
  if (g_sdEpochRef == 0) {
    return 0;
  }
  return g_sdEpochRef + ((uint32_t)(micros() - g_sdEpochSetUs) / 1000000u);
}

// Civil date from a Unix epoch second, for the FAT timestamp.
//
// Local time, not UTC: FAT stores wall-clock time with no zone, so the host
// sends the local epoch and this converts it as given. days_from_civil in
// reverse, from Howard Hinnant's algorithms.
static void sdEpochToCivil(uint32_t epoch, uint16_t *year, uint8_t *month,
                           uint8_t *day, uint8_t *hour, uint8_t *minute,
                           uint8_t *second) {
  uint32_t days = epoch / 86400u;
  uint32_t rem = epoch % 86400u;
  *hour = (uint8_t)(rem / 3600u);
  *minute = (uint8_t)((rem % 3600u) / 60u);
  *second = (uint8_t)(rem % 60u);

  days += 719468u;  // shift the era to 0000-03-01
  const uint32_t era = days / 146097u;
  const uint32_t doe = days % 146097u;
  const uint32_t yoe =
      (doe - doe / 1460u + doe / 36524u - doe / 146096u) / 365u;
  const uint32_t y = yoe + era * 400u;
  const uint32_t doy = doe - (365u * yoe + yoe / 4u - yoe / 100u);
  const uint32_t mp = (5u * doy + 2u) / 153u;
  const uint32_t d = doy - (153u * mp + 2u) / 5u + 1u;
  const uint32_t m = (mp < 10u) ? (mp + 3u) : (mp - 9u);

  *year = (uint16_t)(y + ((m <= 2u) ? 1u : 0u));
  *month = (uint8_t)m;
  *day = (uint8_t)d;
}

// SdFat asks for this on every file create and every close.
void sdDateTimeCallback(uint16_t *date, uint16_t *time) {
  const uint32_t epoch = sdNowEpoch();
  if (epoch == 0) {
    *date = FS_DATE(2000, 1, 1);
    *time = FS_TIME(0, 0, 0);
    return;
  }
  uint16_t year;
  uint8_t month, day, hour, minute, second;
  sdEpochToCivil(epoch, &year, &month, &day, &hour, &minute, &second);
  *date = FS_DATE(year, month, day);
  *time = FS_TIME(hour, minute, second);
}

static const char *sdCodecName(uint16_t codec) {
  switch (codec) {
    case SD_CODEC_NONE: return "none";
    case SD_CODEC_RLE: return "rle";
    default: return "unknown";
  }
}

static void sdSetError(const char *text) {
  snprintf(g_sdState.lastError, sizeof(g_sdState.lastError), "%s", text);
}

// True if name is a plain 8.3 file name in our directory and nothing else.
//
// The host supplies this string. A name carrying a slash or "\.\." would
// reach outside /SPECTRA, so the check is a whitelist of characters rather
// than a search for the sequences that are known to be dangerous.
static bool sdValidName(const char *name) {
  const size_t n = strlen(name);
  if (n == 0 || n >= SD_NAME_MAX) {
    return false;
  }
  for (size_t i = 0; i < n; ++i) {
    const char c = name[i];
    const bool ok = (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') ||
                    c == '.' || c == '_' || c == '-';
    if (!ok) {
      return false;
    }
  }
  return true;
}

static void sdBuildPath(char *out, size_t cap, const char *name) {
  snprintf(out, cap, "%s/%s", SD_DIR, name);
}

// Sequence number encoded in a file name, or 0 if the name is not ours.
static uint32_t sdSeqFromName(const char *name) {
  const size_t n = strlen(name);
  if (n != 12) {  // TTNNNNNN.STK
    return 0;
  }
  if (strcasecmp(name + 8, SD_EXT) != 0) {
    return 0;
  }
  uint32_t seq = 0;
  for (size_t i = 2; i < 8; ++i) {
    if (name[i] < '0' || name[i] > '9') {
      return 0;
    }
    seq = (seq * 10u) + (uint32_t)(name[i] - '0');
  }
  return seq;
}

// Reads the directory once to find where to continue numbering.
//
// Reading it beats storing the next number in a file. A counter file can be
// lost, can be stale after a card is used elsewhere, and has to be kept
// correct on every path out of a write. The directory cannot disagree with
// itself.
static void sdScanDirectory() {
  g_sdState.nextSeq = 1;
  g_sdState.filesAtMount = 0;

  FsFile dir = g_sd.open(SD_DIR, O_RDONLY);
  if (!dir) {
    return;
  }
  FsFile entry;
  char name[64];
  while (entry.openNext(&dir, O_RDONLY)) {
    entry.getName(name, sizeof(name));
    const uint32_t seq = sdSeqFromName(name);
    if (seq != 0) {
      ++g_sdState.filesAtMount;
      if (seq >= g_sdState.nextSeq) {
        g_sdState.nextSeq = seq + 1;
      }
    }
    entry.close();
  }
  dir.close();
}

bool sdMount() {
  if (g_sdState.mounted) {
    return true;
  }
  SPI.setRX(PIN_SD_MISO);
  SPI.setCS(PIN_SD_CS);
  SPI.setSCK(PIN_SD_SCLK);
  SPI.setTX(PIN_SD_MOSI);

  for (uint32_t mhz : SD_CLOCK_TRY_MHZ) {
    // DEDICATED_SPI: nothing else uses SPI0. The ADC is bit-banged on GPIO
    // 8 to 11 and touches no SPI peripheral, so the card keeps the port.
    SdSpiConfig cfg(PIN_SD_CS, DEDICATED_SPI, SD_SCK_MHZ(mhz), &SPI);
    if (g_sd.begin(cfg)) {
      g_sdState.mounted = true;
      g_sdState.clockMhz = mhz;
      g_sdState.lastError[0] = '\0';
      FsDateTime::setCallback(sdDateTimeCallback);
      if (!g_sd.exists(SD_DIR)) {
        g_sd.mkdir(SD_DIR);
      }
      sdScanDirectory();
      return true;
    }
  }
  g_sdState.mounted = false;
  g_sdState.clockMhz = 0;
  sdSetError("no card, or wiring fault");
  return false;
}

void sdUnmount() {
  if (g_sdState.mounted) {
    g_sd.end();
  }
  g_sdState.mounted = false;
  g_sdState.clockMhz = 0;
}

// Assembles the whole record into out, checksum included.
//
// The same two builders the transmitter uses, so a file and a transfer of the
// same capture are identical byte for byte. Not a second encoder: a second
// encoder is a second thing to keep in step, and the wire format is a
// contract with the host.
void sdBuildRecord(uint8_t *out) {
  uint8_t *p = out;
  p += buildStackHeader(p);
  for (uint32_t px = 0; px < PIXEL_COUNT; px += SD_PIXELS_PER_CHUNK) {
    uint32_t take = PIXEL_COUNT - px;
    if (take > SD_PIXELS_PER_CHUNK) take = SD_PIXELS_PER_CHUNK;
    p += buildStackPixels(p, px, take);
  }
  uint32_t checksum = 0;
  const size_t n = (size_t)(p - out);
  for (size_t i = 0; i < n; ++i) checksum += out[i];
  putU32(p, checksum);
}

// Reads one file back into g_sdRecord, whatever form it is stored in.
//
// Three cases, and all three end with a plain record in the buffer:
//
//   a container with codec none  the pixels are copied straight in
//   a container with codec rle   the pixels are decoded
//   no container at all          a file written before compression existed,
//                                which begins with the stack magic instead
//
// The last case is why the container magic differs from the stack magic in
// its third byte. Files already on a card stay readable, and a card can hold
// both kinds at once.
//
// The record's own checksum is verified at the end regardless of the route
// taken. That is the promise the container makes: a corrupt file fails the
// same test a corrupt transfer fails.
bool sdLoadRecord(const char *name) {
  char path[SD_NAME_MAX + sizeof(SD_DIR) + 1];
  sdBuildPath(path, sizeof(path), name);
  FsFile f = g_sd.open(path, O_RDONLY);
  if (!f) {
    sdSetError("not found");
    return false;
  }

  uint8_t head[SD_FILE_HEADER_BYTES];
  bool ok = (f.read(head, sizeof(head)) == (int)sizeof(head));

  if (ok && memcmp(head, STACK_MAGIC, sizeof(STACK_MAGIC)) == 0) {
    // A plain record from before the container existed. The 24 bytes already
    // read are its first 24.
    memcpy(g_sdRecord, head, sizeof(head));
    const size_t rest = STACK_RECORD_BYTES - sizeof(head);
    ok = (f.read(g_sdRecord + sizeof(head), rest) == (int)rest);
    if (!ok) sdSetError("short file");
  } else if (ok && memcmp(head, SD_FILE_MAGIC, sizeof(SD_FILE_MAGIC)) == 0) {
    auto u16at = [&](size_t o) { return (uint16_t)(head[o] | (head[o + 1] << 8)); };
    auto u32at = [&](size_t o) {
      return (uint32_t)head[o] | ((uint32_t)head[o + 1] << 8) |
             ((uint32_t)head[o + 2] << 16) | ((uint32_t)head[o + 3] << 24);
    };
    const uint16_t version = u16at(4);
    const uint16_t codec = u16at(6);
    const uint32_t plainBytes = u32at(8);
    const uint32_t storedBytes = u32at(12);
    const uint16_t stride = u16at(16);
    const uint16_t headerBytes = u16at(18);
    const uint32_t storedSum = u32at(20);

    if (version != SD_FILE_VERSION || plainBytes != STACK_RECORD_BYTES ||
        stride != STACK_PIXEL_BYTES || headerBytes != STACK_HEADER_BYTES ||
        storedBytes > sizeof(g_sdPayload)) {
      sdSetError("unsupported container");
      ok = false;
    }
    if (ok) {
      ok = (f.read(g_sdRecord, STACK_HEADER_BYTES) == (int)STACK_HEADER_BYTES);
      if (!ok) sdSetError("short header");
    }
    if (ok) {
      ok = (f.read(g_sdPayload, storedBytes) == (int)storedBytes);
      if (!ok) sdSetError("short payload");
    }
    if (ok) {
      uint32_t sum = 0;
      for (uint32_t i = 0; i < storedBytes; ++i) sum += g_sdPayload[i];
      if (sum != storedSum) {
        sdSetError("payload checksum");
        ok = false;
      }
    }
    if (ok) {
      uint8_t *pixels = g_sdRecord + STACK_HEADER_BYTES;
      if (codec == SD_CODEC_NONE) {
        if (storedBytes != SD_PIXEL_BYTES) {
          sdSetError("bad payload length");
          ok = false;
        } else {
          memcpy(pixels, g_sdPayload, SD_PIXEL_BYTES);
        }
      } else if (codec == SD_CODEC_RLE) {
        if (sdDecodePixels(g_sdPayload, storedBytes, pixels, PIXEL_COUNT,
                           STACK_PIXEL_BYTES) != SD_PIXEL_BYTES) {
          sdSetError("decode failed");
          ok = false;
        }
      } else {
        sdSetError("unknown codec");
        ok = false;
      }
    }
    if (ok) {
      ok = (f.read(g_sdRecord + STACK_RECORD_BYTES - 4, 4) == 4);
      if (!ok) sdSetError("short trailer");
    }
  } else if (ok) {
    sdSetError("not a spectrum file");
    ok = false;
  } else {
    sdSetError("short file");
  }
  f.close();

  if (!ok) {
    return false;
  }

  uint32_t sum = 0;
  for (size_t i = 0; i < STACK_RECORD_BYTES - 4; ++i) sum += g_sdRecord[i];
  const uint8_t *tail = g_sdRecord + STACK_RECORD_BYTES - 4;
  const uint32_t want = (uint32_t)tail[0] | ((uint32_t)tail[1] << 8) |
                        ((uint32_t)tail[2] << 16) | ((uint32_t)tail[3] << 24);
  if (sum != want) {
    sdSetError("record checksum");
    return false;
  }
  return true;
}

// Writes the current result to a new file.
//
// The record is generated straight into the file the same way it is
// generated into the transmit queue, a chunk at a time, so no second copy of
// it exists in RAM.
//
// Every failure removes the partial file. A short file whose checksum cannot
// pass is worse than no file: it would list, it would transfer, and it would
// fail only at the point where somebody was trying to read the spectrum.
bool sdSaveResult(const char *tag) {
  if (!g_sdState.mounted) {
    sdSetError("not mounted");
    return false;
  }
  if (!capture.haveResult) {
    sdSetError("no result to save");
    return false;
  }
  if (g_sdState.nextSeq > SD_SEQ_MAX) {
    sdSetError("sequence numbers exhausted");
    return false;
  }

  char name[SD_NAME_MAX];
  snprintf(name, sizeof(name), "%c%c%06lu%s", tag[0], tag[1],
           (unsigned long)g_sdState.nextSeq, SD_EXT);
  char path[SD_NAME_MAX + sizeof(SD_DIR) + 1];
  sdBuildPath(path, sizeof(path), name);

  // Assemble the record exactly as the transmitter would, then compress a
  // copy of it. Building it once and reading it twice is what lets the file
  // and the wire carry identical bytes by construction rather than by two
  // pieces of code agreeing with each other.
  sdBuildRecord(g_sdRecord);

  const uint8_t *pixels = g_sdRecord + STACK_HEADER_BYTES;
  uint16_t codec = g_sdState.codec;
  size_t payloadBytes = SD_PIXEL_BYTES;
  const uint8_t *payload = pixels;

  if (codec == SD_CODEC_RLE) {
    const size_t n = sdEncodePixels(pixels, PIXEL_COUNT, STACK_PIXEL_BYTES,
                                    g_sdPayload, sizeof(g_sdPayload));
    // Never write a file that is larger than the record it holds. The coder
    // returns 0 when the output does not fit, and can legitimately produce
    // more bytes than it consumed on data with no runs at all.
    if (n > 0 && n < SD_PIXEL_BYTES) {
      payload = g_sdPayload;
      payloadBytes = n;
    } else {
      codec = SD_CODEC_NONE;
    }
  }

  uint32_t payloadSum = 0;
  for (size_t i = 0; i < payloadBytes; ++i) payloadSum += payload[i];

  uint8_t head[SD_FILE_HEADER_BYTES];
  sdBuildFileHeader(head, codec, (uint32_t)payloadBytes, payloadSum);

  const size_t fileBytes =
      SD_FILE_HEADER_BYTES + STACK_HEADER_BYTES + payloadBytes + 4;

  FsFile f = g_sd.open(path, O_WRONLY | O_CREAT | O_TRUNC);
  if (!f) {
    sdSetError("cannot create file");
    ++g_sdState.writeErrors;
    return false;
  }
  f.preAllocate(fileBytes);

  bool ok = (f.write(head, sizeof(head)) == (int)sizeof(head));
  if (ok) {
    // The record header goes down uncompressed, so a listing can read the
    // capture id, the exposure and the frame counts out of every file
    // without decoding any of them.
    ok = (f.write(g_sdRecord, STACK_HEADER_BYTES) == (int)STACK_HEADER_BYTES);
  }
  if (ok) {
    ok = (f.write(payload, payloadBytes) == (int)payloadBytes);
  }
  if (ok) {
    ok = (f.write(g_sdRecord + STACK_RECORD_BYTES - 4, 4) == 4);
  }
  if (ok) {
    ok = f.sync();  // the last point at which a full card reports itself
  }
  f.close();

  if (!ok) {
    g_sd.remove(path);
    sdSetError("write failed, file removed");
    ++g_sdState.writeErrors;
    return false;
  }

  ++g_sdState.nextSeq;
  ++g_sdState.writes;
  g_sdState.lastPlainBytes = STACK_RECORD_BYTES;
  g_sdState.lastStoredBytes = (uint32_t)fileBytes;
  snprintf(g_sdState.lastFile, sizeof(g_sdState.lastFile), "%s", name);
  g_sdState.lastError[0] = '\0';
  return true;
}

// Called from loop(), not from captureFinish().
//
// A card write takes about 10 ms at 1.6 MB/s and can take far longer when
// the card decides to erase a block. captureFinish() runs inside runFrame(),
// which is the timing-critical path, so the write is deferred one loop pass.
// The completion line also reaches the host first that way, which is what
// the host is waiting on before it asks for the record.
void sdServicePendingSave() {
  if (!g_sdState.savePending) {
    return;
  }
  if (g_txBinaryLock) {
    // A record is going out. txLine() refuses while the lock is held, so
    // the report of this write would be dropped silently. Wait a pass.
    return;
  }
  g_sdState.savePending = false;

  char line[160];
  if (!g_sdState.mounted && !sdMount()) {
    snprintf(line, sizeof(line), "E sd save_failed reason=%s",
             g_sdState.lastError);
    txLine(line);
    return;
  }
  if (sdSaveResult(g_sdState.pendingTag)) {
    snprintf(line, sizeof(line),
             "D saved name=%s bytes=%lu plain=%lu codec=%s id=%lu",
             g_sdState.lastFile, (unsigned long)g_sdState.lastStoredBytes,
             (unsigned long)g_sdState.lastPlainBytes,
             sdCodecName(g_sdState.codec), (unsigned long)capture.id);
  } else {
    snprintf(line, sizeof(line), "E sd save_failed reason=%s",
             g_sdState.lastError);
  }
  txLine(line);
}

// Directory listing.
//
// One line per file, and the values come from the file's own header rather
// than from anything the firmware remembers, so the listing describes the
// card and not this session. start and count page the listing: the transmit
// queue holds about 3 KB and a line is roughly 110 bytes, so an unbounded
// listing of a full card would overrun it.
constexpr uint32_t SD_LIST_MAX = 24;

static void sdEmitFileLine(const char *name, uint32_t bytes, uint16_t date,
                           uint16_t timeField) {
  // Header fields, read straight back out of the file. A file that is too
  // short or carries the wrong magic is still listed, with id=0, rather than
  // hidden: a damaged file that does not appear cannot be deleted either.
  uint32_t id = 0, req = 0, stacked = 0, expUs = 0, durMs = 0;
  uint16_t flags = 0;
  uint16_t codec = SD_CODEC_NONE;
  bool valid = false;

  char path[SD_NAME_MAX + sizeof(SD_DIR) + 1];
  sdBuildPath(path, sizeof(path), name);
  FsFile f = g_sd.open(path, O_RDONLY);
  if (f) {
    uint8_t h[STACK_HEADER_BYTES];
    // A container puts its own 24 byte header first and the record header
    // straight after it, uncompressed for exactly this reason. A file from
    // before compression existed starts with the record header itself.
    bool haveHeader = false;
    uint8_t probe[SD_FILE_HEADER_BYTES];
    if (f.read(probe, sizeof(probe)) == (int)sizeof(probe)) {
      if (memcmp(probe, SD_FILE_MAGIC, sizeof(SD_FILE_MAGIC)) == 0) {
        codec = (uint16_t)(probe[6] | (probe[7] << 8));
        haveHeader = (f.read(h, sizeof(h)) == (int)sizeof(h));
      } else if (memcmp(probe, STACK_MAGIC, sizeof(STACK_MAGIC)) == 0) {
        memcpy(h, probe, sizeof(probe));
        const size_t rest = sizeof(h) - sizeof(probe);
        haveHeader = (f.read(h + sizeof(probe), rest) == (int)rest);
      }
    }
    if (haveHeader &&
        memcmp(h, STACK_MAGIC, sizeof(STACK_MAGIC)) == 0) {
      auto u16at = [&](size_t o) {
        return (uint16_t)(h[o] | (h[o + 1] << 8));
      };
      auto u32at = [&](size_t o) {
        return (uint32_t)h[o] | ((uint32_t)h[o + 1] << 8) |
               ((uint32_t)h[o + 2] << 16) | ((uint32_t)h[o + 3] << 24);
      };
      valid = true;
      flags = u16at(6);
      id = u32at(8);
      req = u32at(12);
      stacked = u32at(20);
      expUs = u32at(28);
      durMs = u32at(32);
    }
    f.close();
  }

  const uint16_t year = (uint16_t)(1980 + (date >> 9));
  const uint8_t month = (uint8_t)((date >> 5) & 0x0F);
  const uint8_t day = (uint8_t)(date & 0x1F);
  const uint8_t hour = (uint8_t)(timeField >> 11);
  const uint8_t minute = (uint8_t)((timeField >> 5) & 0x3F);
  const uint8_t second = (uint8_t)((timeField & 0x1F) * 2);

  char line[200];
  snprintf(line, sizeof(line),
           "D file name=%s bytes=%lu plain=%lu codec=%u tag=%c%c id=%lu req=%lu "
           "stack=%lu exp_us=%lu dur_ms=%lu dark=%d valid=%d "
           "date=%04u-%02u-%02uT%02u:%02u:%02u",
           name, (unsigned long)bytes, (unsigned long)STACK_RECORD_BYTES,
           (unsigned)codec, name[0], name[1], (unsigned long)id,
           (unsigned long)req, (unsigned long)stacked, (unsigned long)expUs,
           (unsigned long)durMs, (int)(flags & 1), (int)valid, year, month,
           day, hour, minute, second);
  txLine(line);
}

void sdList(uint32_t start, uint32_t count) {
  if (count > SD_LIST_MAX) count = SD_LIST_MAX;

  FsFile dir = g_sd.open(SD_DIR, O_RDONLY);
  if (!dir) {
    txLine("E sd no_directory");
    return;
  }

  // Names are collected before any of them is opened. openNext() walks the
  // directory with its own handle, and opening a file inside that walk moves
  // the shared volume cache under it.
  struct Entry {
    char name[SD_NAME_MAX];
    uint32_t bytes;
    uint16_t date;
    uint16_t time;
  };
  static Entry page[SD_LIST_MAX];

  uint32_t index = 0;
  uint32_t taken = 0;
  uint32_t total = 0;
  FsFile entry;
  char name[64];
  while (entry.openNext(&dir, O_RDONLY)) {
    entry.getName(name, sizeof(name));
    if (sdSeqFromName(name) != 0) {
      if (index >= start && taken < count) {
        snprintf(page[taken].name, SD_NAME_MAX, "%s", name);
        page[taken].bytes = (uint32_t)entry.fileSize();
        uint16_t d = 0, t = 0;
        entry.getModifyDateTime(&d, &t);
        page[taken].date = d;
        page[taken].time = t;
        ++taken;
      }
      ++index;
      ++total;
    }
    entry.close();
  }
  dir.close();

  for (uint32_t i = 0; i < taken; ++i) {
    sdEmitFileLine(page[i].name, page[i].bytes, page[i].date, page[i].time);
  }

  char line[96];
  snprintf(line, sizeof(line), "D list_end start=%lu shown=%lu total=%lu",
           (unsigned long)start, (unsigned long)taken, (unsigned long)total);
  txLine(line);
}

// Sends one stored file back to the host.
//
// The file already holds a complete record, so this copies bytes and adds
// nothing. It takes the same lock the result sender takes, for the same
// reason: a text line landing inside the record would split it.
struct FileSender {
  bool active = false;
  uint32_t sent = 0;
  uint32_t total = 0;
  uint32_t startedUs = 0;
};

FileSender fileSender;

constexpr uint32_t SD_SEND_TIMEOUT_US = 10000000;
constexpr size_t SD_SEND_CHUNK = 256;

static void endFileSend() {
  fileSender.active = false;
  g_txBinaryLock = false;
}

// The file is read and decoded in full before the first byte goes out, rather
// than streamed from the card.
//
// It has to be. A compressed file cannot be sent a chunk at a time without
// carrying the decoder state between passes, and the record's checksum cannot
// be verified until the whole of it exists. Reading 12 KB off the card takes
// about 8 ms, which happens once, before the acknowledgement; the transfer
// itself then runs out of RAM at whatever rate the link allows.
bool sdBeginSend(const char *name) {
  if (!sdLoadRecord(name)) {
    return false;
  }
  fileSender.total = (uint32_t)STACK_RECORD_BYTES;
  fileSender.sent = 0;
  fileSender.startedUs = micros();
  fileSender.active = true;
  g_txBinaryLock = true;
  return true;
}

void pumpFileSend() {
  if (!fileSender.active) {
    return;
  }
  if ((uint32_t)(micros() - fileSender.startedUs) > SD_SEND_TIMEOUT_US) {
    endFileSend();  // release the lock before reporting, or the report is lost
    txLine("E sd send_timeout");
    return;
  }

  while (fileSender.sent < fileSender.total) {
    size_t want = fileSender.total - fileSender.sent;
    if (want > SD_SEND_CHUNK) want = SD_SEND_CHUNK;
    if (txFree() < want) {
      return;  // the queue is full; resume on the next pass
    }
    txAppend(g_sdRecord + fileSender.sent, want);
    fileSender.sent += (uint32_t)want;
    pumpTx();
  }
  endFileSend();
  pumpTx();
}

void emitSdStatus(bool withFree) {
  char line[240];
  if (!g_sdState.mounted) {
    snprintf(line, sizeof(line), "D state=absent mounted=0 auto=%d error=%s",
             (int)g_sdState.autoSave,
             g_sdState.lastError[0] ? g_sdState.lastError : "none");
    txLine(line);
    return;
  }

  // The VOLUME, not the card. They are different numbers and only one of
  // them can be filled up.
  //
  // size_mb below is the physical card, from its capacity register.
  // fat_type, the free space and every file are properties of the mounted
  // partition. A card that carries a small partition, or one partitioned by
  // another instrument, reports a large card and a small volume, and
  // "used = size - free" across the two is meaningless. Measured on a 128 GB
  // card reporting FAT16, which no 128 GB volume can be.
  const uint64_t volMb =
      (uint64_t)g_sd.clusterCount() * g_sd.sectorsPerCluster() / 2048u;

  // Free space is not reported unless it is asked for. freeClusterCount()
  // walks the whole file allocation table, which on a large volume takes
  // seconds, and the status line is polled.
  int32_t freeMb = -1;
  if (withFree) {
    const uint32_t freeClusters = g_sd.freeClusterCount();
    const uint32_t sectorsPerCluster = g_sd.sectorsPerCluster();
    // SdFat::freeClusterCount() returns uint32_t, but the volume underneath
    // returns int32_t and uses -1 for an error. The widening turns that into
    // 0xFFFFFFFF, which would be reported as about 2 PB free. Anything above
    // the cluster count is not an answer.
    if (freeClusters == 0xFFFFFFFFu || freeClusters > g_sd.clusterCount()) {
      freeMb = -1;
    } else {
      freeMb = (int32_t)((uint64_t)freeClusters * sectorsPerCluster / 2048u);
    }
  }

  const uint64_t sizeMb = (uint64_t)g_sd.card()->sectorCount() / 2048u;
  snprintf(line, sizeof(line),
           "D state=ok mounted=1 mhz=%lu fat=%d size_mb=%lu vol_mb=%lu "
           "free_mb=%ld "
           "next=%lu files=%lu writes=%lu werr=%lu auto=%d clock=%d "
           "codec=%s codec_n=%u last=%s last_plain=%lu last_stored=%lu "
           "error=%s",
           (unsigned long)g_sdState.clockMhz, (int)g_sd.fatType(),
           (unsigned long)sizeMb, (unsigned long)volMb, (long)freeMb,
           (unsigned long)g_sdState.nextSeq,
           (unsigned long)g_sdState.filesAtMount, (unsigned long)g_sdState.writes,
           (unsigned long)g_sdState.writeErrors, (int)g_sdState.autoSave,
           (int)(g_sdEpochRef != 0), sdCodecName(g_sdState.codec),
           (unsigned)g_sdState.codec,
           g_sdState.lastFile[0] ? g_sdState.lastFile : "none",
           (unsigned long)g_sdState.lastPlainBytes,
           (unsigned long)g_sdState.lastStoredBytes,
           g_sdState.lastError[0] ? g_sdState.lastError : "none");
  txLine(line);
}

// "sd ..." drives the card. Returns true if this function handled cmd.
//
//   sd status [free=1]      report the card and the counters
//   sd mount / sd unmount   attach or release the card
//   sd auto <0|1>           write each finished capture without being asked
//   sd codec [none|rle]     how new files are written, or report it
//   sd time <epoch>         set the clock used for file dates, local seconds
//   sd list [start=<n>] [count=<n>]
//   sd get name=<file>      send one file back as a stack record
//   sd save [tag=<TT>]      write the result now held in RAM
//   sd del name=<file>      delete one file
//   sd rename name=<file> to=<file>   give one file a different name
bool handleSdCommand(const char *cmd) {
  if (strncmp(cmd, "sd", 2) != 0 || (cmd[2] != ' ' && cmd[2] != '\0')) {
    return false;
  }
  const char *rest = (cmd[2] == '\0') ? "status" : cmd + 3;
  char line[160];

  if (strncmp(rest, "status", 6) == 0) {
    uint32_t withFree = 0;
    (void)argU32(rest, "free", &withFree);
    emitSdStatus(withFree != 0);
    return true;
  }

  if (strcmp(rest, "mount") == 0) {
    if (sdMount()) {
      emitSdStatus(false);
    } else {
      snprintf(line, sizeof(line), "E sd mount_failed reason=%s",
               g_sdState.lastError);
      txLine(line);
    }
    return true;
  }

  if (strcmp(rest, "unmount") == 0) {
    if (fileSender.active) {
      txLine("E sd send_in_progress");
      return true;
    }
    sdUnmount();
    txLine("A sd unmounted");
    return true;
  }

  if (strncmp(rest, "auto", 4) == 0) {
    g_sdState.autoSave = (atol(rest + 4) != 0);
    snprintf(line, sizeof(line), "A sd auto=%d", (int)g_sdState.autoSave);
    txLine(line);
    return true;
  }

  if (strncmp(rest, "codec", 5) == 0) {
    const char *arg = rest + 5;
    while (*arg == ' ') ++arg;
    if (*arg != '\0') {
      if (strncmp(arg, "none", 4) == 0) {
        g_sdState.codec = SD_CODEC_NONE;
      } else if (strncmp(arg, "rle", 3) == 0) {
        g_sdState.codec = SD_CODEC_RLE;
      } else {
        txLine("E sd unknown_codec");
        return true;
      }
    }
    // Files already on the card keep the codec they were written with. Each
    // one names its own, so a card may hold both kinds and every one of them
    // still reads back.
    snprintf(line, sizeof(line), "A sd codec=%s n=%u",
             sdCodecName(g_sdState.codec), (unsigned)g_sdState.codec);
    txLine(line);
    return true;
  }

  if (strncmp(rest, "time", 4) == 0) {
    const uint32_t epoch = (uint32_t)strtoul(rest + 4, nullptr, 10);
    if (epoch < 1000000000ul) {  // before 2001, so not a real clock
      txLine("E sd bad_epoch");
      return true;
    }
    g_sdEpochRef = epoch;
    g_sdEpochSetUs = micros();
    uint16_t year;
    uint8_t month, day, hour, minute, second;
    sdEpochToCivil(epoch, &year, &month, &day, &hour, &minute, &second);
    snprintf(line, sizeof(line),
             "A sd time=%04u-%02u-%02uT%02u:%02u:%02u", year, month, day, hour,
             minute, second);
    txLine(line);
    return true;
  }

  // Everything below this point needs the card.
  if (!g_sdState.mounted && !sdMount()) {
    snprintf(line, sizeof(line), "E sd not_mounted reason=%s",
             g_sdState.lastError);
    txLine(line);
    return true;
  }

  if (strncmp(rest, "list", 4) == 0) {
    uint32_t start = 0;
    uint32_t count = SD_LIST_MAX;
    (void)argU32(rest, "start", &start);
    (void)argU32(rest, "count", &count);
    sdList(start, count);
    return true;
  }

  if (strncmp(rest, "get", 3) == 0) {
    if (resultSender.active || fileSender.active) {
      txLine("E sd send_in_progress");
      return true;
    }
    const char *arg = findArg(rest, "name");
    char name[SD_NAME_MAX] = "";
    if (arg != nullptr) {
      size_t i = 0;
      while (i < sizeof(name) - 1 && arg[i] != '\0' && arg[i] != ' ') {
        name[i] = arg[i];
        ++i;
      }
      name[i] = '\0';
    }
    if (!sdValidName(name)) {
      txLine("E sd bad_name");
      return true;
    }
    if (!sdBeginSend(name)) {
      txLine("E sd not_found");
      return true;
    }
    // The acknowledgement goes out BEFORE the lock takes effect, or it would
    // be dropped by the transfer it announces. sdBeginSend() sets the lock,
    // so this line is staged first and the lock released around it.
    g_txBinaryLock = false;
    snprintf(line, sizeof(line), "A sd sending name=%s bytes=%lu", name,
             (unsigned long)fileSender.total);
    txLine(line);
    g_txBinaryLock = true;
    return true;
  }

  if (strncmp(rest, "save", 4) == 0) {
    if (!capture.haveResult) {
      txLine("E sd no_result");
      return true;
    }
    const char *arg = findArg(rest, "tag");
    char tag[3] = "SP";
    if (arg != nullptr && arg[0] > ' ' && arg[1] > ' ') {
      tag[0] = (char)toupper((unsigned char)arg[0]);
      tag[1] = (char)toupper((unsigned char)arg[1]);
    }
    if (sdSaveResult(tag)) {
      snprintf(line, sizeof(line),
               "D saved name=%s bytes=%lu plain=%lu codec=%s id=%lu",
               g_sdState.lastFile, (unsigned long)g_sdState.lastStoredBytes,
               (unsigned long)g_sdState.lastPlainBytes,
               sdCodecName(g_sdState.codec), (unsigned long)capture.id);
    } else {
      snprintf(line, sizeof(line), "E sd save_failed reason=%s",
               g_sdState.lastError);
    }
    txLine(line);
    return true;
  }

  // Renaming is allowed to any valid 8.3 name. The sequence number in the
  // name is not an identity: sdScanDirectory() reads it only to decide where
  // to continue numbering, and a renamed file simply stops contributing to
  // that. The capture id inside the record is the identity, and a rename does
  // not touch the record.
  if (strncmp(rest, "rename", 6) == 0) {
    const char *fromArg = findArg(rest, "name");
    const char *toArg = findArg(rest, "to");
    char from[SD_NAME_MAX] = "";
    char to[SD_NAME_MAX] = "";
    if (fromArg != nullptr) {
      size_t i = 0;
      while (i < sizeof(from) - 1 && fromArg[i] != '\0' && fromArg[i] != ' ') {
        from[i] = fromArg[i];
        ++i;
      }
      from[i] = '\0';
    }
    if (toArg != nullptr) {
      size_t i = 0;
      while (i < sizeof(to) - 1 && toArg[i] != '\0' && toArg[i] != ' ') {
        to[i] = (char)toupper((unsigned char)toArg[i]);
        ++i;
      }
      to[i] = '\0';
    }
    if (!sdValidName(from) || !sdValidName(to)) {
      txLine("E sd bad_name");
      return true;
    }
    char fromPath[SD_NAME_MAX + sizeof(SD_DIR) + 1];
    char toPath[SD_NAME_MAX + sizeof(SD_DIR) + 1];
    sdBuildPath(fromPath, sizeof(fromPath), from);
    sdBuildPath(toPath, sizeof(toPath), to);
    if (!g_sd.exists(fromPath)) {
      txLine("E sd not_found");
      return true;
    }
    // Refuse rather than overwrite. The target holds someone's measurement.
    if (g_sd.exists(toPath)) {
      txLine("E sd name_taken");
      return true;
    }
    if (!g_sd.rename(fromPath, toPath)) {
      txLine("E sd rename_failed");
      return true;
    }
    // The name the next capture takes is read from the directory, so a
    // rename can free a number. Read it again rather than leave the counter
    // pointing past a name that no longer exists.
    sdScanDirectory();
    snprintf(line, sizeof(line), "D renamed name=%s to=%s", from, to);
    txLine(line);
    return true;
  }

  if (strncmp(rest, "del", 3) == 0) {
    const char *arg = findArg(rest, "name");
    char name[SD_NAME_MAX] = "";
    if (arg != nullptr) {
      size_t i = 0;
      while (i < sizeof(name) - 1 && arg[i] != '\0' && arg[i] != ' ') {
        name[i] = arg[i];
        ++i;
      }
      name[i] = '\0';
    }
    if (!sdValidName(name)) {
      txLine("E sd bad_name");
      return true;
    }
    char path[SD_NAME_MAX + sizeof(SD_DIR) + 1];
    sdBuildPath(path, sizeof(path), name);
    if (!g_sd.exists(path)) {
      txLine("E sd not_found");
      return true;
    }
    if (!g_sd.remove(path)) {
      txLine("E sd delete_failed");
      return true;
    }
    snprintf(line, sizeof(line), "D deleted name=%s", name);
    txLine(line);
    return true;
  }

  txLine("E sd unknown_subcommand");
  return true;
}

// Raises the flag that sdServicePendingSave() acts on. Called from
// captureFinish(), which runs inside runFrame() and must not touch the card.
void sdRequestSave(const char *tag) {
  if (!g_sdState.autoSave) {
    return;
  }
  g_sdState.pendingTag[0] = tag[0];
  g_sdState.pendingTag[1] = tag[1];
  g_sdState.pendingTag[2] = '\0';
  g_sdState.savePending = true;
}

void emitCaptureStatus() {
  const char *state = (g_mode == MODE_CAPTURE)
                          ? "run"
                          : (capture.haveResult ? "done" : "idle");
  const uint32_t elapsedMs =
      (g_mode == MODE_CAPTURE)
          ? (uint32_t)((micros() - capture.startUs) / 1000u)
          : capture.durationMs;

  // 320, not 240. Every counter here is a uint32 and prints up to ten
  // digits, so the worst case line is about 240 characters and would have
  // been truncated exactly when the counters mattered most.
  char line[320];
  snprintf(line, sizeof(line),
           "C state=%s id=%lu req=%lu att=%lu stack=%lu excl=%lu exp_us=%lu "
           "elapsed_ms=%lu result=%d mode=%d block=%lu sigma_x100=%lu "
           "miss=%lu stale=%lu badbits=%lu late_us=%lu nostart=%lu",
           state, (unsigned long)capture.id,
           (unsigned long)capture.framesRequested,
           (unsigned long)capture.framesAttempted,
           (unsigned long)capture.framesStacked,
           (unsigned long)capture.framesExcluded,
           (unsigned long)capture.exposureUs, (unsigned long)elapsedMs,
           (int)capture.haveResult, (int)g_mode,
           (unsigned long)capture.blockFrames,
           (unsigned long)capture.sigmaX100, (unsigned long)capture.statMissing,
           (unsigned long)capture.statStale, (unsigned long)capture.statBadBits,
           (unsigned long)capture.statMaxLateUs,
           (unsigned long)capture.statNoStart);
  txLine(line);
}

// "cap ..." drives a stacked capture. Returns true if this function handled
// cmd.
//
//   cap start secs=<s> exp=<us> [frames=<n>] [sigma=<f>] [block=<n>]
//             [iters=<n>] [dark=<0|1>]
//   cap stop
//   cap status
//   cap get
//
// Either secs or frames sets the length. secs is the natural request and is
// converted here rather than on the host, so the frame count and the exposure
// that produced it cannot disagree.
bool handleCaptureCommand(const char *cmd) {
  if (strncmp(cmd, "cap ", 4) != 0) {
    return false;
  }
  const char *rest = cmd + 4;

  if (strncmp(rest, "start", 5) == 0) {
    if (g_mode == MODE_CAPTURE) {
      txLine("E cap already_running");
      return true;
    }
    if (resultSender.active) {
      txLine("E cap send_in_progress");
      return true;
    }

    uint32_t exposureUs = (g_framePeriodUs > 0) ? g_framePeriodUs : FRAME_PERIOD_US;
    (void)argU32(rest, "exp", &exposureUs);
    exposureUs = clampU32(exposureUs, FRAME_PERIOD_MIN_US, FRAME_PERIOD_MAX_US);

    uint32_t frames = 0;
    float secs = 0.0f;
    if (!argU32(rest, "frames", &frames)) {
      if (!argFloat(rest, "secs", &secs)) {
        txLine("E cap need_secs_or_frames");
        return true;
      }
      if (secs < 0.0f) secs = 0.0f;
      if (secs > (float)CAPTURE_MAX_SECONDS) secs = (float)CAPTURE_MAX_SECONDS;
      frames = (uint32_t)(((secs * 1000000.0f) / (float)exposureUs) + 0.5f);
    }
    frames = clampU32(frames, 1, CAPTURE_MAX_FRAMES);

    uint32_t block = STACK_BLOCK_FRAMES_DEFAULT;
    (void)argU32(rest, "block", &block);
    block = clampU32(block, STACK_BLOCK_FRAMES_MIN, STACK_BLOCK_FRAMES_MAX);

    float sigma = (float)STACK_SIGMA_X100_DEFAULT / 100.0f;
    (void)argFloat(rest, "sigma", &sigma);
    uint32_t sigmaX100 = clampU32((uint32_t)((sigma * 100.0f) + 0.5f),
                                  STACK_SIGMA_X100_MIN, STACK_SIGMA_X100_MAX);

    uint32_t iters = STACK_CLIP_ITERS_DEFAULT;
    (void)argU32(rest, "iters", &iters);
    iters = clampU32(iters, 1, STACK_CLIP_ITERS_MAX);

    uint32_t dark = 0;
    (void)argU32(rest, "dark", &dark);

    // The name prefix for the card file. It defaults from the dark flag so a
    // host that knows nothing about the card still produces sensible names.
    char tag[3] = "SP";
    if (dark != 0) {
      tag[0] = 'D';
      tag[1] = 'K';
    }
    const char *tagArg = findArg(rest, "tag");
    if (tagArg != nullptr && tagArg[0] > ' ' && tagArg[1] > ' ') {
      tag[0] = (char)toupper((unsigned char)tagArg[0]);
      tag[1] = (char)toupper((unsigned char)tagArg[1]);
    }

    captureBegin(frames, exposureUs, sigmaX100, block, iters, dark != 0);
    // After captureBegin(), which resets the rest of the capture state.
    capture.sdTag[0] = tag[0];
    capture.sdTag[1] = tag[1];
    capture.sdTag[2] = '\0';

    // The reply names every value actually in use, including the ones the
    // host did not send and the ones that were clamped. A capture that quietly
    // ran at a different exposure from the one requested would be impossible
    // to detect afterwards.
    char line[200];
    snprintf(line, sizeof(line),
             "A cap started id=%lu frames=%lu exp_us=%lu secs=%lu.%03lu "
             "sigma_x100=%lu block=%lu iters=%lu dark=%d warmup=%lu tag=%s",
             (unsigned long)capture.id, (unsigned long)frames,
             (unsigned long)exposureUs,
             (unsigned long)(((uint64_t)frames * exposureUs) / 1000000ull),
             (unsigned long)((((uint64_t)frames * exposureUs) / 1000ull) % 1000ull),
             (unsigned long)sigmaX100, (unsigned long)block,
             (unsigned long)iters, (int)(dark != 0),
             (unsigned long)CAPTURE_WARMUP_FRAMES, capture.sdTag);
    txLine(line);
    return true;
  }

  if (strcmp(rest, "stop") == 0) {
    if (g_mode != MODE_CAPTURE) {
      txLine("E cap not_running");
      return true;
    }
    // Everything collected so far is kept. A stopped capture is a shorter
    // capture, not a failed one, and framesStacked in the record says how
    // short it was.
    captureFinish(true);
    return true;
  }

  if (strcmp(rest, "status") == 0) {
    emitCaptureStatus();
    return true;
  }

  if (strcmp(rest, "get") == 0) {
    if (g_mode == MODE_CAPTURE) {
      txLine("E cap still_running");
      return true;
    }
    if (!capture.haveResult) {
      txLine("E cap no_result");
      return true;
    }
    if (resultSender.active) {
      txLine("E cap send_in_progress");
      return true;
    }
    // The acknowledgement goes out BEFORE the lock is taken, or it would be
    // dropped by its own transfer.
    char line[80];
    snprintf(line, sizeof(line), "A cap sending id=%lu bytes=%lu",
             (unsigned long)capture.id, (unsigned long)STACK_RECORD_BYTES);
    txLine(line);
    beginResultSend();
    return true;
  }

  txLine("E cap unknown_subcommand");
  return true;
}

void handleCommand(const char *cmd) {
  if (strcmp(cmd, "id") == 0 || strcmp(cmd, "ver") == 0) {
    emitDeviceInfo();
    return;
  }

  if (strncmp(cmd, "rate ", 5) == 0) {
    if (g_mode == MODE_CAPTURE) {
      txLine("E rate busy_capturing");
      return;
    }
    long hz = atol(cmd + 5);
    if (hz < 0) hz = 0;
    if (hz > (long)FRAME_RATE_MAX_HZ) hz = FRAME_RATE_MAX_HZ;
    if (hz != 0 && hz < (long)FRAME_RATE_MIN_HZ) hz = FRAME_RATE_MIN_HZ;
    g_frameRateHz = (uint32_t)hz;
    if (hz == 0) {
      g_framePeriodUs = 0;  // always due: the frames run one after the other
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

  // The period uses microseconds. Whole hertz are too coarse at the low end:
  // 1 Hz and 2 Hz are 1000 ms and 500 ms, with no value between them.
  if (strncmp(cmd, "period ", 7) == 0) {
    // A capture owns the frame period until it finishes, and restores it
    // afterwards. Changing it part way through would leave the result record
    // naming an exposure that only some of its frames had.
    if (g_mode == MODE_CAPTURE) {
      txLine("E period busy_capturing");
      return;
    }
    long us = atol(cmd + 7);
    if (us != 0) {
      if (us < (long)FRAME_PERIOD_MIN_US) us = FRAME_PERIOD_MIN_US;
      if (us > (long)FRAME_PERIOD_MAX_US) us = FRAME_PERIOD_MAX_US;
    }
    g_framePeriodUs = (uint32_t)us;
    g_frameRateHz = (us > 0) ? (uint32_t)(1000000L / us) : 0;  // for the reply only
    char line[80];
    snprintf(line, sizeof(line), "A frame_us=%lu rate_hz=%lu",
             (unsigned long)g_framePeriodUs, (unsigned long)g_frameRateHz);
    txLine(line);
    return;
  }

  // Stops and starts frame transmission without stopping the scan. The
  // scan keeps running in either case, so the detector never sits
  // integrating with nothing to flush it and the first frame after "live 1"
  // is as valid as any other.
  if (strncmp(cmd, "live ", 5) == 0) {
    if (g_mode == MODE_CAPTURE) {
      txLine("E live busy_capturing");
      return;
    }
    g_mode = (atol(cmd + 5) != 0) ? MODE_LIVE : MODE_QUIET;
    char line[48];
    snprintf(line, sizeof(line), "A live=%d", (int)(g_mode == MODE_LIVE));
    txLine(line);
    return;
  }

  if (handleCalCommand(cmd)) {
    return;
  }

  if (handleCaptureCommand(cmd)) {
    return;
  }

  if (handleSdCommand(cmd)) {
    return;
  }

  if (handleDiagnosticCommand(cmd)) {
    return;
  }

  char line[96];
  snprintf(line, sizeof(line), "E unknown_command=%.48s", cmd);
  txLine(line);
}

// Reads the bytes that arrived, then returns. A reply goes into the same
// queue as the frames, so the two cannot interleave.
//
// Do not stop command handling while that queue holds data. An earlier
// version did. With frames one after the other, the queue is never empty, and
// the device stopped answering.
void pollHostCommands() {
  // 384, not 96. An over-long line is discarded rather than truncated, so a
  // request that did not fit would simply never arrive. A capture request is
  // about 60 characters, but "cal set" carries CAL_TEXT_MAX of reference
  // points behind an eight character verb, so the buffer has to hold both.
  static char buf[384];
  static uint8_t len = 0;

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
      len = 0;  // the line is too long: discard it
    }
  }
}

// ===========================================================================
// Entry points
// ===========================================================================

void setup() {
  pinMode(PIN_START, OUTPUT);
  digitalWrite(PIN_START, LOW);

  pinMode(PIN_ADC_CS, OUTPUT);
  digitalWrite(PIN_ADC_CS, HIGH);

  pinMode(PIN_ADC_MISO, INPUT);
  pinMode(PIN_ADC_MOSI, OUTPUT);
  // A high SDI level sends command 1111b, which the datasheet calls default
  // mode. The ADC then needs no command decoding. This pin was low before,
  // which sent command 0000b. Each conversion then read as zero, although the
  // analog inputs were correct.
  digitalWrite(PIN_ADC_MOSI, HIGH);

  pinMode(PIN_ADC_SCLK, OUTPUT);
  digitalWrite(PIN_ADC_SCLK, LOW);  // firmware drives SCLK for each transfer

  Serial.begin(115200);
  // USB CDC reads false until the host asserts DTR. This loop therefore waits
  // for a terminal to open the port, so the boot lines cannot scroll past
  // unseen. Capture has not started.
  while (!Serial) {
    delay(10);
  }
  delay(100);

  g_txFifoCapacity = Serial.availableForWrite();  // nothing queued yet
  Serial.println("BOOT: RP2350 firmware active");
  Serial.println("BOOT: PIO debounces the pixel trigger, not an interrupt");
  printPioBuildStamp();

  // Before emitDeviceInfo(), because the "I " line reports how many
  // calibration points the device holds. Mounted here rather than on the
  // first request so that a store which will not mount is a line in the boot
  // log. Failure is not fatal: the instrument runs uncalibrated and the host
  // keeps its own copy.
  g_cal.fsReady = LittleFS.begin();
  calLoad();
  if (!g_cal.fsReady) {
    Serial.println("BOOT: no calibration store (flash filesystem would not "
                   "mount)");
  } else if (g_cal.points >= 2) {
    Serial.printf("BOOT: calibration loaded, %lu points\n",
                  (unsigned long)g_cal.points);
  } else {
    Serial.println("BOOT: no calibration stored");
  }

  // The SPI self check, then the ADC reset. Both run on every boot, and
  // before emitDeviceInfo(), because the "I " line reports the result.
  //
  // They run at boot for two reasons. The check is the only thing that tests
  // the SDI direction, and an SDI fault is otherwise silent: the line stuck
  // high sends command 1111b, which the part answers with plausible
  // conversion data, so a broken instrument produces normal-looking spectra.
  // Running the check only when someone remembers to type "sditest" means it
  // is run after the data is already suspect, not before it is taken.
  //
  // The reset is what puts the part into a known state. Firmware wrote no ADC
  // configuration at all before this, so the first scan after a power cycle
  // used whatever the ADC happened to come up in. The reset also consumes the
  // all-ones cycle the datasheet says SDO produces immediately afterwards;
  // without that, the first pixel read returns ADC_POST_RESET_WORD, which is
  // full scale and looks like a real bright spike.
  //
  // Cost is a handful of bit-banged 16-bit transfers plus one 50 us delay, so
  // this is tens of microseconds of a boot that already waits for the host to
  // open the port. Nothing here waits on an external signal, so a missing or
  // dead ADC makes it report FAIL rather than hang.
  //
  // Repeat it at any time with "selftest".
  if (!runSelfTest()) {
    Serial.println("BOOT: self test FAILED; see the H sditest and H adcreset "
                   "lines above");
  }

  emitDeviceInfo();

  // Mount the card now rather than on the first capture. A card that is
  // absent or miswired is then a line in the boot log, not a capture that
  // completes and quietly stores nothing. Failure is not fatal: the
  // instrument runs without a card, and "sd mount" retries at any time.
  if (sdMount()) {
    Serial.printf("BOOT: SD card mounted at %lu MHz, next file %06lu\n",
                  (unsigned long)g_sdState.clockMhz,
                  (unsigned long)g_sdState.nextSeq);
  } else {
    Serial.printf("BOOT: no SD card (%s); captures will not be stored\n",
                  g_sdState.lastError);
  }

  // Firmware writes no other ADC configuration here. See adcSoftwareReset().

  // The pull-up is necessary. PIO reads the same physical pad, so the line
  // must idle high.
  pinMode(PIN_PIXEL_TRIG, INPUT_PULLUP);
  initPixelTrigPio();
  initRawEdgeCountPio();

  const uint32_t nowUs = micros();
  g_nextFrameStartUs = nowUs;
  runFrame(nowUs);
}

void loop() {
  const uint32_t nowUs = micros();

  if ((int32_t)(nowUs - g_nextFrameStartUs) >= 0) {
    runFrame(nowUs);
  } else {
    drainFilteredTriggers();
    // Opportunistic only. runFrame() already ran the slice that guarantees a
    // block finishes before the next one needs the buffer; this lets a long
    // gap finish it sooner.
    if (g_mode == MODE_CAPTURE) {
      stackProcessSlice(capture.sliceP);
    }
  }

  pumpTx();
  // The result record is three times the size of the queue, so it is fed in
  // a chunk at a time from here rather than staged like a frame.
  pumpResult();
  // A file coming back off the card is fed in the same way, for the same
  // reason.
  pumpFileSend();
  // A finished capture is written to the card here, not in captureFinish().
  // The write can stall for tens of milliseconds and captureFinish() runs
  // inside runFrame().
  sdServicePendingSave();
  // Commands run between frames, never during a scan. A command therefore
  // cannot disturb the pixel timing.
  pollHostCommands();
}
