// Standalone DS3231 real-time clock bring-up test, on a bit-banged I2C bus.
//
// This file is NOT part of the spectrometer firmware. It is a separate
// program, built by its own PlatformIO environment (see platformio.ini,
// [env:rtc_test]). Flash it, open the serial monitor at 115200, read the
// report.
//
//   pio run -e rtc_test -t upload
//   pio device monitor -e rtc_test
//
// WHY THIS BUS IS BIT-BANGED.
//
// The board has SCL on GPIO 20 and SDA on GPIO 21. The RP2350 cannot do that
// in hardware. Its I2C0 block accepts SDA only on an even pin and SCL only on
// an odd one, so GPIO 20 can be SDA and GPIO 21 can be SCL, and no setting
// reverses them. The check is in the framework, in TwoWire::setSCL, and it
// does not return an error: an illegal pin calls panic(), so a hardware I2C
// build with these pins hard-faults at boot rather than reporting a fault.
//
// So this file drives the bus itself. That is not a workaround for a broken
// part, it is the correct response to the pins that exist, and the same
// answer this project already reached for the ADC in main.cpp. Software I2C
// costs speed, which matters here not at all: the whole conversation with a
// clock is a dozen bytes, once.
//
// WHICH PIN IS WHICH IS NOT ASSUMED. The test tries GPIO 20 as the clock,
// and if nothing answers it tries GPIO 21 as the clock instead, then reports
// which one worked. A swapped pair and an absent device look identical from
// the outside, so this file settles it by experiment rather than by trusting
// a schematic that has already been wrong once.
//
// No RTC library either. The DS3231 is about a dozen registers of packed
// binary coded decimal, and this file exists to prove the wiring and the
// part, so every register access is in plain sight. See the DS3231 datasheet,
// table 1.
//
// The card test in sd_test.cpp is the sibling of this one. The two exist for
// the same reason: the instrument needs the time to date the files it writes
// to the card, and today the host supplies it over the serial link with
// "sd time". A DS3231 removes that dependency.

#include <Arduino.h>
#include <string.h>
#include <stdlib.h>

// ---------------------------------------------------------------------------
// Hardware configuration
// ---------------------------------------------------------------------------

// The two pins the clock is on. WHICH IS WHICH IS DECIDED AT RUN TIME, by
// trying both and seeing which one a device answers on. Being wrong about it
// looks exactly like a clock that is not there, and the wiring is the thing
// this test is least able to assume.
constexpr uint8_t PIN_RTC_20 = 20;
constexpr uint8_t PIN_RTC_21 = 21;

static uint8_t g_pinScl = PIN_RTC_20;
static uint8_t g_pinSda = PIN_RTC_21;

constexpr uint8_t DS3231_ADDR = 0x68;
// Most DS3231 breakout boards, the ZS-042 in particular, carry an AT24C32
// EEPROM as well. Finding it confirms the bus works; not finding it is not a
// fault.
constexpr uint8_t AT24C32_ADDR = 0x57;

// Half a bit period, in microseconds. 5 us is about 100 kHz, which is the
// speed every I2C device supports and far faster than this test needs.
//
// Adjustable at run time with "speed <us>", because the achievable speed is a
// property of the wiring rather than of the code: it is set by how hard the
// pull-up resistors can charge the capacitance of the lines. A module with
// 4.7k pull-ups will run at 2 us. Relying on the chip's own pull-ups, which
// are about fifty times weaker, it will not.
static uint32_t g_halfUs = 5;

// Longest wait for a device that is holding the clock line down. Clock
// stretching is legitimate and the DS3231 barely uses it, but a line stuck
// low by a fault looks identical at first and must not hang the program.
constexpr uint32_t I2C_STRETCH_TIMEOUT_US = 2000;

// Register map, from the datasheet.
constexpr uint8_t REG_SECONDS = 0x00;
constexpr uint8_t REG_CONTROL = 0x0E;
constexpr uint8_t REG_STATUS = 0x0F;
constexpr uint8_t REG_AGING = 0x10;
constexpr uint8_t REG_TEMP = 0x11;

constexpr uint8_t CTRL_EOSC = 0x80;   // 1 = oscillator STOPS on battery
constexpr uint8_t CTRL_CONV = 0x20;   // force a temperature conversion now
constexpr uint8_t STAT_OSF = 0x80;    // the oscillator stopped at some point
constexpr uint8_t STAT_BSY = 0x04;    // a temperature conversion is running

// Datasheet: a conversion takes 125 ms typical, 200 ms maximum.
constexpr uint32_t TEMP_CONVERSION_TIMEOUT_MS = 400;

struct RtcTime {
  uint16_t year = 2000;  // full year, not the register's two digits
  uint8_t month = 1;     // 1 to 12
  uint8_t day = 1;       // 1 to 31
  uint8_t hour = 0;      // 0 to 23 always; 12 hour mode is converted away
  uint8_t minute = 0;
  uint8_t second = 0;
  uint8_t weekday = 1;   // 1 to 7, this file uses 1 = Sunday
};

// ---------------------------------------------------------------------------
// Bit-banged I2C master
//
// I2C lines are open drain: a device pulls a line to ground or lets go of it,
// and a pull-up resistor raises it. Nothing ever drives a line high. Doing so
// would fight whatever else is driving it low, which on this bus means
// shorting an output of the RP2350 against an output of the clock.
//
// So "high" here means letting go, which is INPUT, and "low" means OUTPUT
// LOW. Releasing a line enables the chip's internal pull-up as a safety net
// for a board with no pull-up resistors fitted. Note what that costs: a line
// released this way reads HIGH even with nothing connected to the pin, so
// "both lines are high" is not evidence that anything is attached.
// hasExternalPullup() is the check that actually distinguishes them.
// ---------------------------------------------------------------------------

static inline void i2cDelay() { delayMicroseconds(g_halfUs); }

static inline void sdaHigh() { pinMode(g_pinSda, INPUT_PULLUP); }
static inline void sclHigh() { pinMode(g_pinScl, INPUT_PULLUP); }

static inline void sdaLow() {
  digitalWrite(g_pinSda, LOW);   // set the level BEFORE driving the pin,
  pinMode(g_pinSda, OUTPUT);     // or the pin glitches high on the way out
}

static inline void sclLow() {
  digitalWrite(g_pinScl, LOW);
  pinMode(g_pinScl, OUTPUT);
}

static inline bool sdaRead() { return digitalRead(g_pinSda) != LOW; }
static inline bool sclRead() { return digitalRead(g_pinScl) != LOW; }

// Releases the clock line and waits for it to actually rise.
//
// Returns false if it never does. A device may hold the clock down to ask for
// time, which is legal, and a shorted line holds it down for ever, which is
// not; the two are indistinguishable until the timeout expires, so there has
// to be one.
static bool sclHighAndWait() {
  sclHigh();
  const uint32_t startUs = micros();
  while (!sclRead()) {
    if ((uint32_t)(micros() - startUs) > I2C_STRETCH_TIMEOUT_US) {
      return false;
    }
  }
  i2cDelay();
  return true;
}

static void i2cIdle() {
  sdaHigh();
  sclHigh();
  i2cDelay();
}

static bool i2cStart() {
  sdaHigh();
  if (!sclHighAndWait()) return false;
  sdaLow();          // SDA falls while SCL is high: that is a START
  i2cDelay();
  sclLow();
  i2cDelay();
  return true;
}

// A repeated START, used between the register address and the data of a read.
// A STOP followed by a START would work on this part, but it releases the bus
// in between, and on a bus with more than one master that is the gap in which
// the transfer is lost.
static bool i2cRestart() {
  sdaHigh();
  i2cDelay();
  if (!sclHighAndWait()) return false;
  sdaLow();
  i2cDelay();
  sclLow();
  i2cDelay();
  return true;
}

static bool i2cStop() {
  sdaLow();
  i2cDelay();
  if (!sclHighAndWait()) return false;
  sdaHigh();         // SDA rises while SCL is high: that is a STOP
  i2cDelay();
  return true;
}

// Sends one byte, most significant bit first. Returns true if the device
// acknowledged it.
static bool i2cWriteByte(uint8_t value) {
  for (uint8_t bit = 0; bit < 8; ++bit) {
    if (value & 0x80) {
      sdaHigh();
    } else {
      sdaLow();
    }
    value = (uint8_t)(value << 1);
    i2cDelay();
    if (!sclHighAndWait()) return false;
    sclLow();
    i2cDelay();
  }

  // The ninth clock is the acknowledgement, and the device drives it. Let go
  // of SDA first or the master is the one answering its own question.
  sdaHigh();
  i2cDelay();
  if (!sclHighAndWait()) return false;
  const bool acked = !sdaRead();   // the device pulls SDA LOW to acknowledge
  sclLow();
  i2cDelay();
  return acked;
}

// Reads one byte. `ack` false sends a NACK, which tells the device that this
// was the last byte wanted; a read that acknowledges its final byte leaves
// the device still trying to send.
static uint8_t i2cReadByte(bool ack) {
  uint8_t value = 0;
  sdaHigh();
  for (uint8_t bit = 0; bit < 8; ++bit) {
    i2cDelay();
    if (!sclHighAndWait()) return 0xFF;
    value = (uint8_t)((value << 1) | (sdaRead() ? 1 : 0));
    sclLow();
  }

  if (ack) {
    sdaLow();
  } else {
    sdaHigh();
  }
  i2cDelay();
  sclHighAndWait();
  sclLow();
  i2cDelay();
  sdaHigh();
  return value;
}

// Frees a bus that a device is holding down.
//
// A slave interrupted mid-byte, by a reset of this board for instance, can be
// left driving SDA low and waiting for the clock pulses that would finish its
// transfer. It will hold the whole bus that way for ever. Nine clock pulses
// carry it past the end of any byte, and a STOP then returns it to idle.
// Worth doing at every start, because the fault it clears is invisible: the
// scan simply finds nothing.
static bool i2cRecover() {
  i2cIdle();
  if (sdaRead()) {
    return true;   // the line is already free
  }
  for (uint8_t i = 0; i < 9 && !sdaRead(); ++i) {
    sclLow();
    i2cDelay();
    sclHigh();
    i2cDelay();
  }
  i2cStop();
  i2cIdle();
  return sdaRead();
}

// True if a device answers at this address.
static bool i2cPing(uint8_t addr) {
  if (!i2cStart()) {
    return false;
  }
  const bool acked = i2cWriteByte((uint8_t)(addr << 1));  // write bit clear
  i2cStop();
  return acked;
}

// ---------------------------------------------------------------------------
// DS3231 register access
// ---------------------------------------------------------------------------

static bool rtcWrite(uint8_t reg, const uint8_t *data, size_t n) {
  if (!i2cStart()) return false;
  bool ok = i2cWriteByte((uint8_t)(DS3231_ADDR << 1));
  ok = ok && i2cWriteByte(reg);
  for (size_t i = 0; ok && i < n; ++i) {
    ok = i2cWriteByte(data[i]);
  }
  i2cStop();
  return ok;
}

static bool rtcRead(uint8_t reg, uint8_t *data, size_t n) {
  if (n == 0) return false;
  if (!i2cStart()) return false;

  bool ok = i2cWriteByte((uint8_t)(DS3231_ADDR << 1));
  ok = ok && i2cWriteByte(reg);
  ok = ok && i2cRestart();
  ok = ok && i2cWriteByte((uint8_t)((DS3231_ADDR << 1) | 1));
  if (!ok) {
    i2cStop();
    return false;
  }
  for (size_t i = 0; i < n; ++i) {
    data[i] = i2cReadByte(i + 1 < n);   // NACK the last byte
  }
  i2cStop();
  return true;
}

static bool rtcWriteReg(uint8_t reg, uint8_t value) {
  return rtcWrite(reg, &value, 1);
}

static bool rtcReadReg(uint8_t reg, uint8_t *value) {
  return rtcRead(reg, value, 1);
}

// ---------------------------------------------------------------------------
// Calendar arithmetic
//
// Howard Hinnant's civil-from-days pair. Present because the test has to
// restore the clock to the time it held before, corrected for however long
// the test itself took, and because a Unix epoch is what a host would send.
// ---------------------------------------------------------------------------

static int32_t daysFromCivil(int32_t y, uint32_t m, uint32_t d) {
  y -= (m <= 2) ? 1 : 0;
  const int32_t era = (y >= 0 ? y : y - 399) / 400;
  const uint32_t yoe = (uint32_t)(y - era * 400);
  const uint32_t doy = (153u * (m + (m > 2 ? -3u : 9u)) + 2u) / 5u + d - 1u;
  const uint32_t doe = yoe * 365u + yoe / 4u - yoe / 100u + doy;
  return era * 146097 + (int32_t)doe - 719468;
}

static void civilFromDays(int32_t z, uint16_t *y, uint8_t *m, uint8_t *d) {
  z += 719468;
  const int32_t era = (z >= 0 ? z : z - 146096) / 146097;
  const uint32_t doe = (uint32_t)(z - era * 146097);
  const uint32_t yoe =
      (doe - doe / 1460u + doe / 36524u - doe / 146096u) / 365u;
  const int32_t yr = (int32_t)yoe + era * 400;
  const uint32_t doy = doe - (365u * yoe + yoe / 4u - yoe / 100u);
  const uint32_t mp = (5u * doy + 2u) / 153u;
  const uint32_t dd = doy - (153u * mp + 2u) / 5u + 1u;
  const uint32_t mm = (mp < 10u) ? (mp + 3u) : (mp - 9u);
  *y = (uint16_t)(yr + ((mm <= 2u) ? 1 : 0));
  *m = (uint8_t)mm;
  *d = (uint8_t)dd;
}

static uint32_t toEpoch(const RtcTime &t) {
  const int32_t days = daysFromCivil(t.year, t.month, t.day);
  return (uint32_t)days * 86400u + t.hour * 3600u + t.minute * 60u + t.second;
}

static RtcTime fromEpoch(uint32_t epoch) {
  RtcTime t;
  const int32_t days = (int32_t)(epoch / 86400u);
  const uint32_t rem = epoch % 86400u;
  civilFromDays(days, &t.year, &t.month, &t.day);
  t.hour = (uint8_t)(rem / 3600u);
  t.minute = (uint8_t)((rem % 3600u) / 60u);
  t.second = (uint8_t)(rem % 60u);
  // 1970-01-01 was a Thursday. The DS3231 does not care what the numbers
  // mean, only that they advance, so this file fixes 1 as Sunday.
  t.weekday = (uint8_t)(((days + 4) % 7 + 7) % 7 + 1);
  return t;
}

static const char *weekdayName(uint8_t w) {
  static const char *const NAMES[] = {"?", "Sun", "Mon", "Tue",
                                      "Wed", "Thu", "Fri", "Sat"};
  return (w >= 1 && w <= 7) ? NAMES[w] : "?";
}

// ---------------------------------------------------------------------------
// Time registers
// ---------------------------------------------------------------------------

static uint8_t bcdToBin(uint8_t v) {
  return (uint8_t)((v >> 4) * 10 + (v & 0x0F));
}

static uint8_t binToBcd(uint8_t v) {
  return (uint8_t)(((v / 10) << 4) | (v % 10));
}

// Reads all seven time registers in ONE transaction, deliberately.
//
// The DS3231 copies the running time into a user buffer when a read begins,
// so a single burst cannot catch the clock half way through a carry. Seven
// separate reads can, and the failure is rare enough to look like a random
// glitch months later: read 23:59:59, the clock rolls, then read the date and
// get tomorrow's.
static bool rtcReadTime(RtcTime *out) {
  uint8_t r[7];
  if (!rtcRead(REG_SECONDS, r, sizeof(r))) {
    return false;
  }
  out->second = bcdToBin((uint8_t)(r[0] & 0x7F));
  out->minute = bcdToBin((uint8_t)(r[1] & 0x7F));

  if (r[2] & 0x40) {
    // 12 hour mode. Bit 5 is PM. Converted away here, so nothing above this
    // function has to know the mode exists.
    uint8_t h = bcdToBin((uint8_t)(r[2] & 0x1F));
    const bool pm = (r[2] & 0x20) != 0;
    if (h == 12) h = 0;
    out->hour = (uint8_t)(pm ? h + 12 : h);
  } else {
    out->hour = bcdToBin((uint8_t)(r[2] & 0x3F));
  }

  out->weekday = (uint8_t)(r[3] & 0x07);
  out->day = bcdToBin((uint8_t)(r[4] & 0x3F));
  out->month = bcdToBin((uint8_t)(r[5] & 0x1F));
  // Bit 7 of the month register is the century flag. It toggles when the year
  // register wraps from 99 to 00, which this test reads as 2000 to 2100.
  const uint16_t century = (r[5] & 0x80) ? 2100 : 2000;
  out->year = (uint16_t)(century + bcdToBin(r[6]));
  return true;
}

static bool rtcWriteTime(const RtcTime &t) {
  if (t.year < 2000 || t.year > 2199) {
    return false;
  }
  const uint8_t century = (t.year >= 2100) ? 0x80 : 0x00;
  uint8_t r[7];
  r[0] = binToBcd(t.second);
  r[1] = binToBcd(t.minute);
  r[2] = binToBcd(t.hour);   // bit 6 clear: 24 hour mode, always
  r[3] = (uint8_t)(t.weekday & 0x07);
  r[4] = binToBcd(t.day);
  r[5] = (uint8_t)(binToBcd(t.month) | century);
  r[6] = binToBcd((uint8_t)(t.year % 100));
  return rtcWrite(REG_SECONDS, r, sizeof(r));
}

// Temperature, in hundredths of a degree Celsius.
//
// The DS3231 measures its own die to steer the oscillator, and reading it
// back is the cheapest proof that the part really is a DS3231. A DS1307, a
// PCF8523 and most other clocks answer at address 0x68 too, and a module sold
// as one thing is sometimes the other.
static bool rtcReadTemperatureC100(int32_t *out) {
  uint8_t r[2];
  if (!rtcRead(REG_TEMP, r, sizeof(r))) {
    return false;
  }
  // A 10-bit two's complement value: the whole degrees are a signed byte at
  // 11h and the fraction is the top two bits of 12h, worth a quarter degree
  // each. Adding a POSITIVE fraction to a NEGATIVE whole is correct and is
  // not an oversight; the two fields are not sign and magnitude, they are the
  // high and low halves of one signed number. -0.25 C arrives as 0xFF and
  // 0b11, which is -1 whole plus three quarters.
  const int8_t whole = (int8_t)r[0];
  const uint8_t quarters = (uint8_t)(r[1] >> 6);
  *out = (int32_t)whole * 100 + (int32_t)quarters * 25;
  return true;
}

// Forces a conversion and waits for it to finish.
//
// Without this the test can read a stale number and call it a pass. The part
// converts once every 64 seconds, and on the first application of power the
// temperature registers read 0 C until the first conversion completes about
// 200 ms later. A test that runs immediately after reset would see exactly
// 0.00 C, which is inside every sanity check anybody would write.
//
// The datasheet's sequence: check BSY before starting, then set CONV, which
// stays 1 until the conversion finishes.
static bool rtcConvertTemperature() {
  uint8_t status = 0;
  const uint32_t startMs = millis();
  do {
    if (!rtcReadReg(REG_STATUS, &status)) {
      return false;
    }
    if (!(status & STAT_BSY)) {
      break;
    }
    delay(5);
  } while ((millis() - startMs) < TEMP_CONVERSION_TIMEOUT_MS);
  if (status & STAT_BSY) {
    return false;
  }

  uint8_t control = 0;
  if (!rtcReadReg(REG_CONTROL, &control)) {
    return false;
  }
  if (!rtcWriteReg(REG_CONTROL, (uint8_t)(control | CTRL_CONV))) {
    return false;
  }

  const uint32_t convStartMs = millis();
  while ((millis() - convStartMs) < TEMP_CONVERSION_TIMEOUT_MS) {
    if (!rtcReadReg(REG_CONTROL, &control)) {
      return false;
    }
    if (!(control & CTRL_CONV)) {
      return true;   // CONV clears itself when the conversion completes
    }
    delay(5);
  }
  return false;
}

static void printTime(const char *label, const RtcTime &t) {
  Serial.printf("%s %04u-%02u-%02u %02u:%02u:%02u %s\n", label, t.year,
                t.month, t.day, t.hour, t.minute, t.second,
                weekdayName(t.weekday));
}

static bool sameTime(const RtcTime &a, const RtcTime &b) {
  return a.year == b.year && a.month == b.month && a.day == b.day &&
         a.hour == b.hour && a.minute == b.minute && a.second == b.second;
}

// ---------------------------------------------------------------------------
// The test
// ---------------------------------------------------------------------------

// Is anything actually pulling this line up, other than us?
//
// This is the test that matters, and its absence is why an earlier version of
// this file gave misleading advice. Releasing a line means INPUT_PULLUP,
// which switches on the RP2350's OWN pull-up, so a completely disconnected
// pin reads high and looks perfect. "Both lines idle high" therefore proves
// nothing at all about whether a clock is attached.
//
// So: drive the line low, then release it to plain INPUT with no pull-up of
// any kind, and see whether it comes back up.
//
//   it rises      something external is pulling it up. Every DS3231 module
//                 carries 4.7k resistors, so this means the module is there
//                 and it has power.
//   it stays low  nothing is pulling it up. The module is absent, unpowered,
//                 or that pin is not connected to it.
//
// 4.7k against the 400 pF the I2C specification allows rises in about 5 us,
// so 40 us is a wide margin. With nothing but leakage to charge the line, it
// stays down for many milliseconds.
static bool hasExternalPullup(uint8_t pin) {
  digitalWrite(pin, LOW);
  pinMode(pin, OUTPUT);
  delayMicroseconds(50);
  pinMode(pin, INPUT);          // no internal pull-up: this is the point
  delayMicroseconds(40);
  const bool rose = digitalRead(pin) != LOW;
  pinMode(pin, INPUT_PULLUP);   // back to the idle state
  return rose;
}

// Checks the lines before any transfer is attempted.
//
// Both must idle high. Which one is low says what is wrong, and none of it is
// visible from a failed transfer alone:
//
//   both low      no power to the module, or the two lines shorted together
//   SDA low only  a device left mid-transfer, which i2cRecover() can clear
//   SCL low only  a short to ground; nothing in software fixes that
static bool checkLines() {
  i2cIdle();
  delayMicroseconds(50);
  const bool scl = sclRead();
  const bool sda = sdaRead();

  if (scl && sda) {
    // Both read high, which on its own means nothing: the chip's own pull-up
    // does that for a pin connected to no board at all. Ask who is really
    // holding them up.
    const bool pullScl = hasExternalPullup(g_pinScl);
    const bool pullSda = hasExternalPullup(g_pinSda);
    Serial.printf("lines   both idle high; external pull-up on GPIO %u %s, "
                  "GPIO %u %s\n",
                  g_pinScl, pullScl ? "yes" : "NO", g_pinSda,
                  pullSda ? "yes" : "NO");
    if (pullScl && pullSda) {
      Serial.println("        a powered module is on both pins");
      return true;
    }
    Serial.println("[!]     Only this chip's internal pull-up is holding a");
    Serial.println("[!]     line high, so nothing is attached to it. Every");
    Serial.println("[!]     DS3231 module has 4.7k resistors on SCL and SDA.");
    Serial.println("[!]     Check 3V3 and GND to the module first: with no");
    Serial.println("[!]     power its pull-ups have nothing to pull up to.");
    Serial.println("[!]     Then check the two signal wires themselves.");
    // Not a hard failure. The scan still runs, because the internal pull-ups
    // are weak but not useless, and a device that answers through them is
    // worth knowing about.
    return true;
  }

  Serial.printf("lines   SCL %s, SDA %s\n", scl ? "high" : "LOW",
                sda ? "high" : "LOW");
  if (!sda && scl) {
    Serial.println("        A device is holding the data line. Clocking it");
    Serial.println("        past the end of its transfer...");
    if (i2cRecover()) {
      Serial.println("        recovered, the line is free again");
      return true;
    }
    Serial.println("[!]     still held. Power the module down and up.");
    return false;
  }
  if (!scl) {
    Serial.println("[!]     The clock line is held low. That is wiring, not");
    Serial.printf("        software: check GPIO %u for a short to ground.\n",
                  g_pinScl);
  }
  Serial.println("[!]     Check 3V3 and ground to the module, and that SCL");
  Serial.printf("[!]     is on GPIO %u and SDA on GPIO %u.\n", g_pinScl,
                g_pinSda);
  return false;
}

// Every address on the bus, not only the one wanted.
//
// A scan that finds nothing and a scan that finds a device at an unexpected
// address are different faults with different fixes, and a test that only
// says "not found" cannot tell them apart. A bus whose two lines are swapped,
// which is the mistake this whole file exists because of, finds nothing.
static void scanBus() {
  Serial.println("scan    addresses that answer:");
  uint32_t found = 0;
  for (uint8_t addr = 0x08; addr < 0x78; ++addr) {
    if (i2cPing(addr)) {
      const char *what = "";
      if (addr == DS3231_ADDR) what = "  DS3231 clock";
      if (addr == AT24C32_ADDR) what = "  AT24C32 EEPROM on the same module";
      Serial.printf("        0x%02X%s\n", addr, what);
      ++found;
    }
  }
  if (found == 0) {
    Serial.println("        none");
    Serial.println("        With the lines idling high, nothing answering");
    Serial.println("        usually means SCL and SDA are swapped, or the");
    Serial.println("        module has no power.");
  }
}

static void reportRegisters() {
  uint8_t control = 0, status = 0, aging = 0;
  if (!rtcReadReg(REG_CONTROL, &control) || !rtcReadReg(REG_STATUS, &status) ||
      !rtcReadReg(REG_AGING, &aging)) {
    Serial.println("regs    FAILED: cannot read the control registers");
    return;
  }
  Serial.printf("regs    control 0x%02X  status 0x%02X  aging %+d\n", control,
                status, (int)(int8_t)aging);

  if (status & STAT_OSF) {
    Serial.println("[!]     the oscillator stopped since the time was last");
    Serial.println("        set, so the time it holds is NOT trustworthy.");
    Serial.println("        Usually a dead or absent backup cell. This test");
    Serial.println("        clears the flag when it sets the time.");
  } else {
    Serial.println("        oscillator has run without interruption");
  }
  if (control & CTRL_EOSC) {
    Serial.println("[!]     EOSC is set: the clock STOPS on battery power.");
    Serial.println("        Cleared below, or the clock loses time whenever");
    Serial.println("        the board is unpowered.");
  }
}

static bool testTemperature() {
  if (!rtcConvertTemperature()) {
    Serial.println("temp    FAILED: the conversion never finished");
    return false;
  }
  int32_t c100 = 0;
  if (!rtcReadTemperatureC100(&c100)) {
    Serial.println("temp    FAILED: no answer from the temperature registers");
    return false;
  }
  // Printed from the sign and the magnitude separately. Integer division
  // truncates towards zero, so -25 hundredths divided by 100 is 0, and the
  // sign would be lost: a die at -0.25 C would print as 0.25 C.
  const char *sign = (c100 < 0) ? "-" : "";
  const int32_t mag = (c100 < 0) ? -c100 : c100;
  Serial.printf("temp    %s%ld.%02ld C (the die, not the room)\n", sign,
                (long)(mag / 100), (long)(mag % 100));
  // A DS1307 has no temperature sensor and returns whatever those addresses
  // happen to hold, which is how a mislabelled module gives itself away.
  if (c100 < -4000 || c100 > 8500) {
    Serial.println("[!]     that is outside the part's own range. This may");
    Serial.println("        not be a DS3231.");
    return false;
  }
  return true;
}

// How long to wait for the clock to carry, and how far it should get.
//
// 3500 ms for a 3 second advance, NOT 3000. Writing the seconds register
// resets the part's countdown chain, so the first second after a write
// elapses from the moment of the write. Seconds therefore increment at
// 1.000 s, 2.000 s and 3.000 s after it, and a 3000 ms wait lands on the
// third of those, arriving a few milliseconds late or early depending on how
// long the read-back before it took. That is a coin toss between +2 and +3
// on every run: a test that fails perhaps a third of the time, for a reason
// nothing in its output would explain.
//
// Waiting 3500 ms puts the read in the middle of the +3 second, half a second
// clear of the boundary on either side.
constexpr uint32_t ROLLOVER_WAIT_MS = 3500;
constexpr uint32_t ROLLOVER_ADVANCE_S = 3;

// Writes a time, reads it back, and compares every field.
//
// The times chosen are not arbitrary. Both sit two seconds before a month
// boundary, so the clock has to carry seconds into minutes into hours into
// days into months on its own while the test watches. One is in 2026, which
// is not a leap year, and the other in 2028, which is: a part that ignores
// leap years passes the first and fails the second, and a bring-up test that
// only ever wrote 12:00:00 would find neither fault for years.
static bool testRollover(const char *label, RtcTime set, RtcTime expect) {
  if (!rtcWriteTime(set)) {
    Serial.printf("%s  FAILED: the write was not acknowledged\n", label);
    return false;
  }

  RtcTime back;
  if (!rtcReadTime(&back)) {
    Serial.printf("%s  FAILED: cannot read back\n", label);
    return false;
  }
  if (!sameTime(set, back)) {
    Serial.printf("%s  FAILED: wrote and read different times\n", label);
    printTime("        wrote", set);
    printTime("        read ", back);
    return false;
  }

  delay(ROLLOVER_WAIT_MS);

  if (!rtcReadTime(&back)) {
    Serial.printf("%s  FAILED: cannot read after the wait\n", label);
    return false;
  }
  if (!sameTime(expect, back)) {
    Serial.printf("%s  FAILED: the carry is wrong\n", label);
    printTime("        expected", expect);
    printTime("        got     ", back);
    return false;
  }
  Serial.printf("%s  OK: %04u-%02u-%02u %02u:%02u:%02u became "
                "%04u-%02u-%02u %02u:%02u:%02u\n",
                label, set.year, set.month, set.day, set.hour, set.minute,
                set.second, back.year, back.month, back.day, back.hour,
                back.minute, back.second);
  return true;
}

static bool testRunning() {
  RtcTime a, b;
  if (!rtcReadTime(&a)) {
    Serial.println("tick    FAILED: cannot read");
    return false;
  }
  delay(2200);
  if (!rtcReadTime(&b)) {
    Serial.println("tick    FAILED: cannot read again");
    return false;
  }
  const int32_t moved = (int32_t)toEpoch(b) - (int32_t)toEpoch(a);
  if (moved < 1 || moved > 4) {
    Serial.printf("tick    FAILED: %ld s passed over a 2.2 s wait\n",
                  (long)moved);
    Serial.println("        A stopped clock reads the same twice. Check EOSC");
    Serial.println("        in the control register and the backup cell.");
    return false;
  }
  Serial.printf("tick    OK: advanced %ld s over a 2.2 s wait\n", (long)moved);
  return true;
}

static void runAllTests() {
  Serial.println();
  Serial.println("=== DS3231 clock test, bit-banged I2C ===");
  Serial.printf("pins    SCL %u, SDA %u, half period %lu us\n", g_pinScl,
                g_pinSda, (unsigned long)g_halfUs);

  if (!checkLines()) {
    Serial.println("RESULT  FAIL (the bus is not usable)");
    Serial.println("Send r to run the test again.");
    return;
  }

  // Settle the pin order by experiment rather than by reading the schematic.
  //
  // A swapped pair is indistinguishable from a missing device from the
  // outside: both lines still idle high, and nothing answers. Rather than ask
  // which way round the board is, try one way, then the other, and say which
  // one worked. The cost is one extra scan and it removes a question that has
  // already been answered wrongly once.
  if (!i2cPing(DS3231_ADDR)) {
    Serial.printf("clock   nothing at 0x%02X with SCL %u, SDA %u\n",
                  DS3231_ADDR, g_pinScl, g_pinSda);
    const uint8_t t = g_pinScl;
    g_pinScl = g_pinSda;
    g_pinSda = t;
    i2cIdle();
    Serial.printf("        trying the other way round, SCL %u, SDA %u\n",
                  g_pinScl, g_pinSda);
    if (i2cPing(DS3231_ADDR)) {
      Serial.println("[!]     THAT is the wiring. The two lines are the");
      Serial.println("[!]     opposite of what this file assumed. Change");
      Serial.println("[!]     g_pinScl and g_pinSda at the top of");
      Serial.println("[!]     src/rtc_test.cpp so it starts correctly.");
    } else {
      // Sweep every address, in BOTH pin orders, before giving up.
      //
      // Worth doing because the DS3231 address cannot be anything else. It
      // is fixed at 1101000 in the silicon; pins 5 to 12 are no-connects, so
      // there are no address straps to get wrong and no second DS3231 that
      // could ever share a bus. So a sweep that finds nothing at either
      // orientation has genuinely eliminated the entire bus, and what is
      // left is electrical: no supply, no ground, or wires not reaching
      // these two pins.
      //
      // A sweep that finds SOMETHING is the more useful outcome. An answer
      // at 0x57 alone is the EEPROM on the same module, which proves the
      // bus, the power and the ground are all fine and indicts the clock
      // itself.
      Serial.println("scan    sweeping every address, both ways round:");
      Serial.printf("        with SCL %u, SDA %u\n", g_pinScl, g_pinSda);
      scanBus();
      g_pinSda = g_pinScl;
      g_pinScl = t;
      i2cIdle();
      Serial.printf("        with SCL %u, SDA %u\n", g_pinScl, g_pinSda);
      scanBus();

      Serial.printf("clock   FAILED: nothing at 0x%02X either way round\n",
                    DS3231_ADDR);
      Serial.println("        The address is not the problem: a DS3231 is");
      Serial.println("        always 0x68 and has no address pins, and the");
      Serial.println("        sweep above covered every address anyway.");
      Serial.println("        If the sweep found the EEPROM at 0x57 but no");
      Serial.println("        clock, the wiring is good and the clock chip");
      Serial.println("        is not. If it found nothing at all:");
      Serial.println("        - with no external pull-up reported above, the");
      Serial.println("          module has no power or no ground;");
      Serial.println("        - with pull-ups present, the two signal wires");
      Serial.println("          do not reach GPIO 20 and 21.");
      Serial.println("        A coin cell alone is enough to answer, so a");
      Serial.println("        module with a good battery that stays silent");
      Serial.println("        is not simply unpowered.");
      Serial.println("RESULT  FAIL (no clock)");
      Serial.println("Send r to run again, or swap to flip the two pins.");
      return;
    }
  }
  Serial.printf("clock   OK, answers at 0x%02X with SCL %u, SDA %u\n",
                DS3231_ADDR, g_pinScl, g_pinSda);

  scanBus();

  reportRegisters();

  bool ok = testTemperature();

  // Everything from here writes to the clock, so the time it is holding has
  // to come back afterwards. Saved with the millis() reading that goes with
  // it, so the restore can add back however long the test took rather than
  // putting the clock back ten seconds slow.
  RtcTime original;
  const bool haveOriginal = rtcReadTime(&original);
  const uint32_t originalAtMs = millis();
  if (haveOriginal) {
    printTime("time    now", original);
  } else {
    Serial.println("time    FAILED: cannot read the current time");
    ok = false;
  }

  // 2026 is not a leap year: 28 February is the last day of that month.
  RtcTime a{};
  a.year = 2026; a.month = 2; a.day = 28;
  a.hour = 23; a.minute = 59; a.second = 58;
  a.weekday = fromEpoch(toEpoch(a)).weekday;
  ok &= testRollover("month ", a,
                     fromEpoch(toEpoch(a) + ROLLOVER_ADVANCE_S));

  // 2028 is a leap year: 29 February exists and the clock must find it.
  RtcTime b{};
  b.year = 2028; b.month = 2; b.day = 28;
  b.hour = 23; b.minute = 59; b.second = 58;
  b.weekday = fromEpoch(toEpoch(b)).weekday;
  ok &= testRollover("leap  ", b,
                     fromEpoch(toEpoch(b) + ROLLOVER_ADVANCE_S));

  ok &= testRunning();

  // Clear the two flags that make a clock lose time, now that a known-good
  // time is about to be written. OSF stays set from the last power loss until
  // something clears it, so a clock never told otherwise reports itself
  // untrustworthy for ever.
  uint8_t control = 0, status = 0;
  if (rtcReadReg(REG_CONTROL, &control) && rtcReadReg(REG_STATUS, &status)) {
    rtcWriteReg(REG_CONTROL, (uint8_t)(control & ~CTRL_EOSC));
    rtcWriteReg(REG_STATUS, (uint8_t)(status & ~STAT_OSF));
  }

  if (haveOriginal) {
    const uint32_t elapsedS = (millis() - originalAtMs + 500u) / 1000u;
    const RtcTime restored = fromEpoch(toEpoch(original) + elapsedS);
    if (rtcWriteTime(restored)) {
      printTime("time    restored to", restored);
      Serial.printf("        (the original plus the %lu s this test took)\n",
                    (unsigned long)elapsedS);
    } else {
      Serial.println("[!]     could not restore the original time");
      ok = false;
    }
  }

  Serial.println(ok ? "RESULT  PASS" : "RESULT  FAIL");
  Serial.println("Commands: r, now, set <epoch>, speed <us>, scan, swap");
  Serial.println("To set the clock from this computer, in LOCAL seconds:");
  Serial.println("  python -c \"import time;"
                 "print(int(time.time()-time.timezone))\"");
}

// ---------------------------------------------------------------------------
// Entry points
// ---------------------------------------------------------------------------

static void handleCommand(const char *cmd) {
  if (strcmp(cmd, "r") == 0 || strcmp(cmd, "R") == 0) {
    runAllTests();
    return;
  }

  if (strcmp(cmd, "swap") == 0) {
    const uint8_t t = g_pinScl;
    g_pinScl = g_pinSda;
    g_pinSda = t;
    i2cIdle();
    Serial.printf("swap    SCL is now GPIO %u, SDA GPIO %u\n", g_pinScl,
                  g_pinSda);
    Serial.println("        Send r to retest. This lasts until the next");
    Serial.println("        reset; edit the file to make it permanent.");
    return;
  }

  if (strcmp(cmd, "scan") == 0) {
    checkLines();
    scanBus();
    return;
  }

  if (strcmp(cmd, "now") == 0) {
    RtcTime t;
    if (rtcReadTime(&t)) {
      printTime("time   ", t);
      Serial.printf("        epoch %lu\n", (unsigned long)toEpoch(t));
    } else {
      Serial.println("time    FAILED: no answer");
    }
    return;
  }

  if (strncmp(cmd, "speed ", 6) == 0) {
    const uint32_t us = (uint32_t)strtoul(cmd + 6, nullptr, 10);
    if (us < 1 || us > 500) {
      Serial.println("speed   FAILED: give 1 to 500 microseconds");
      return;
    }
    g_halfUs = us;
    Serial.printf("speed   half period %lu us, about %lu kHz\n",
                  (unsigned long)g_halfUs,
                  (unsigned long)(500u / g_halfUs));
    Serial.println("        Send r to retest at this speed. A speed the");
    Serial.println("        pull-up resistors cannot support fails as a");
    Serial.println("        clock that does not answer at all.");
    return;
  }

  if (strncmp(cmd, "set ", 4) == 0) {
    const uint32_t epoch = (uint32_t)strtoul(cmd + 4, nullptr, 10);
    // Before 2001 is not a real clock reading, it is a host that sent
    // something else. Writing it would look like success.
    if (epoch < 1000000000ul) {
      Serial.println("set     FAILED: that is not a plausible epoch");
      return;
    }
    const RtcTime t = fromEpoch(epoch);
    if (!rtcWriteTime(t)) {
      Serial.println("set     FAILED: the write was not acknowledged");
      return;
    }
    uint8_t status = 0;
    if (rtcReadReg(REG_STATUS, &status)) {
      rtcWriteReg(REG_STATUS, (uint8_t)(status & ~STAT_OSF));
    }
    printTime("set     OK, clock now", t);
    return;
  }

  Serial.println("Commands: r (run the test), now, set <epoch>, "
                 "speed <us>, scan, swap");
}

void setup() {
  Serial.begin(115200);

  // Both lines released before anything else. A pin left as an output from a
  // previous program would fight the module the moment it powers up.
  i2cIdle();

  // Wait for the host to open the port, but not for an unlimited time. The
  // board must still run when a wall supply powers it and no host is
  // connected.
  const uint32_t startMs = millis();
  while (!Serial && (millis() - startMs) < 3000) {
    delay(10);
  }

  runAllTests();
}

void loop() {
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
  delay(10);
}
