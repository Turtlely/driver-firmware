#include <Arduino.h>
#include <hardware/gpio.h>
#include <hardware/sync.h>
#include <hardware/pio.h>
#include <hardware/clocks.h>
#include <hardware/timer.h>
#include <hardware/structs/sio.h>
#include <pico/platform.h>
#include "pixel_trig.pio.h"

// Hardware configuration.

constexpr uint8_t PIN_START = 7;       // OUTPUT: starts one CPLD scan cycle
constexpr uint8_t PIN_PIXEL_TRIG = 1;  // INPUT:  one pulse per pixel, watched by PIO
constexpr uint8_t PIN_ADC_SCLK = 10;   // OUTPUT: SPI clock
constexpr uint8_t PIN_ADC_MISO = 8;    // INPUT:  ADS7279 SDO
constexpr uint8_t PIN_ADC_MOSI = 11;   // OUTPUT: ADS7279 SDI, held high (see setup)
constexpr uint8_t PIN_ADC_CS = 9;      // OUTPUT: ADS7279 chip select

constexpr uint16_t ADC_CMD_READ_CFR = 0xC000; // ????
constexpr uint16_t ADC_CMD_WRITE_CFR = 0xE000; // ???
constexpr uint16_t ADC_CFR_MASK = 0x0FFF; // ???
constexpr uint16_t ADC_CFR_DEFAULT = 0xFFD;  // all ones except D1, which reads 0
constexpr uint16_t ADC_CFR_RESET = 0xFFE;    // D0 low: assert software reset
constexpr uint16_t ADC_CFR_NORMAL = 0xFFF;   // D0 high: normal operation

constexpr uint16_t ADC_CODE_MASK = 0x3FFF;  // 14-bit result, left-justified on the wire
constexpr uint32_t ADC_VREF_MV = 2500;      // REF5025 reference voltage
constexpr uint32_t ADC_FULL_SCALE = 1u << 14; // ???

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
// 318 character max limit. +1 newline.
bool txLine(const char *text) {
  const size_t n = strlen(text); // # of characters in text, each character is 1 byte
  if (n + 1 > txFree()) { // if # of characters + /n char exceeds the number of free bytes left in queue. 
    return false; // doesnt fit so we dont do anything
  }
  char buf[320]; // define character buffer that can hold 320 bytes max
  // silently truncates the text to fit in the buffer
  const size_t take = (n < sizeof(buf) - 2) ? n : sizeof(buf) - 2; // if n < 318 chars, take=n. else, take=318
  memcpy(buf, text, take); // copy that text packet into the buffer
  buf[take] = '\n'; // append a newline to the end
  return txAppend(buf, take + 1); // TODO can probably remove the +1 here?
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


// shorthand for software reset of the ADC
void adcSoftwareReset() {
  adcCommand(ADC_CMD_WRITE_CFR | ADC_CFR_RESET);
  delayMicroseconds(50);
  adcCommand(ADC_CMD_WRITE_CFR | ADC_CFR_NORMAL);
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

void reportFilteredTriggerCount() {
  if (!filteredTriggerCount.active) {
    return;
  }

  const int32_t delta =
      (int32_t)filteredTriggerCount.eventCount - (int32_t)PIXEL_COUNT;
  char line[160];
  snprintf(line, sizeof(line),
           "E frame %lu filtered %lu expected %lu delta %+ld "
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
  static char frameBuf[FRAME_BINARY_BYTES + 448];
  if (txFree() >= sizeof(frameBuf)) {
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
// Host command interface
//
// The device uses plain ASCII requests and replies on the frame stream port.
// The host application reads the "I " line to identify the device and to get
// the acquisition parameters. That format is a contract. You can add a
// key=value field. Do not rename or reorder an existing field.
// ===========================================================================

constexpr char FW_VERSION[] = "1.1.0";
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
           (unsigned long)g_pixelPeriodUs, (unsigned long)g_framePeriodUs,
           (unsigned long)FRAME_PERIOD_MIN_US, (unsigned long)FRAME_PERIOD_MAX_US,
           (unsigned long)REFRACTORY_US,
           (unsigned long)(clock_get_hz(clk_sys) / 1000000u),
           (unsigned long)pioChecksum, (unsigned)MISSING_PIXEL, buildDate);
  txLine(line);
}

// Hardware test commands. None of them runs unless you send it. Returns true
// if this function handled cmd.
bool handleDiagnosticCommand(const char *cmd) {
  // Measures the low time of the trigger pulse. This identifies the device
  // that drives the line. Approximately 857 ns is the ADS7279 EOC output.
  // Approximately 5 us is the CPLD nTRIG output.
  //
  // The measurement calibrates itself. It counts loop passes for both halves
  // of one period, then scales by the known 20 us period. Only the ratio
  // matters, so the cost of one loop pass cancels.
  //
  // Do not call micros() inside the timing loops. A timer read costs more
  // than the interval to measure.
  if (strcmp(cmd, "pulsewidth") == 0) {
    constexpr uint32_t MASK = 1u << PIN_PIXEL_TRIG;
    constexpr uint32_t SAMPLES = 8;
    constexpr uint32_t GUARD = 4000000u;  // limit, in case the line stops
    uint32_t lowCounts[SAMPLES] = {0};
    uint32_t highCounts[SAMPLES] = {0};

    pio_sm_clear_fifos(g_pixelTrigPio, g_pixelTrigSm);
    pio_sm_clear_fifos(g_pixelTrigPio, g_rawEdgeSm);
    digitalWrite(PIN_START, HIGH);
    delayMicroseconds(START_PULSE_WIDTH_US);
    digitalWrite(PIN_START, LOW);

    const uint32_t irqState = save_and_disable_interrupts();
    for (uint32_t i = 0; i < SAMPLES; ++i) {
      uint32_t guard = 0;
      while ((sio_hw->gpio_in & MASK) == 0 && ++guard < GUARD) {}
      guard = 0;
      while ((sio_hw->gpio_in & MASK) != 0 && ++guard < GUARD) {}
      uint32_t low = 0, high = 0;
      while ((sio_hw->gpio_in & MASK) == 0 && ++low < GUARD) {}
      while ((sio_hw->gpio_in & MASK) != 0 && ++high < GUARD) {}
      lowCounts[i] = low;
      highCounts[i] = high;
    }
    restore_interrupts(irqState);

    char line[220];
    int off = snprintf(line, sizeof(line), "A pulsewidth lowns");
    for (uint32_t i = 0; i < SAMPLES; ++i) {
      const uint32_t total = lowCounts[i] + highCounts[i];
      const uint32_t ns =
          total ? (uint32_t)(((uint64_t)20000u * lowCounts[i]) / total) : 0;
      off += snprintf(line + off, sizeof(line) - off, " %lu", (unsigned long)ns);
    }
    snprintf(line + off, sizeof(line) - off, " (lo0=%lu hi0=%lu)",
             (unsigned long)lowCounts[0], (unsigned long)highCounts[0]);
    txLine(line);
    return true;
  }

  // Sets the idle level of SDI. adcReadPixel() does not drive MOSI, so
  // this changes the command that each pixel read sends. If SDI reaches the
  // ADC, then raw16 must change too.
  if (strncmp(cmd, "mosi ", 5) == 0) {
    const bool high = (atol(cmd + 5) != 0);
    digitalWrite(PIN_ADC_MOSI, high);
    char line[48];
    snprintf(line, sizeof(line), "A mosi=%d", (int)high);
    txLine(line);
    return true;
  }

  // Reads the CFR several times. This is the only test of the SDI direction,
  // because a pixel read tests SDO only. A stable ADC_CFR_DEFAULT value
  // proves that SDI works.
  if (strcmp(cmd, "cfr") == 0) {
    char line[160];
    int off = snprintf(line, sizeof(line), "A cfr");
    uint32_t matches = 0;
    for (int i = 0; i < 8; ++i) {
      const uint16_t value = adcCommand(ADC_CMD_READ_CFR) & ADC_CFR_MASK;
      if (value == ADC_CFR_DEFAULT) ++matches;
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
    snprintf(line, sizeof(line), "A cfrw wrote=%03X readback=%03X %s",
             (unsigned)value, (unsigned)readback,
             (value == readback) ? "MATCH" : "MISMATCH");
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

  if (strcmp(cmd, "adcreset") == 0) {
    adcSoftwareReset();
    txLine("A adcreset=done");
    return true;
  }

  return false;
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
  static char buf[48];
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
  emitDeviceInfo();

  // Firmware writes no ADC configuration here. See adcSoftwareReset().

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
  }

  pumpTx();
  // Commands run between frames, never during a scan. A command therefore
  // cannot disturb the pixel timing.
  pollHostCommands();
}
