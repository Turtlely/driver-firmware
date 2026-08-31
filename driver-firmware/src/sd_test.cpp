// Standalone SD card bring-up test.
//
// This file is NOT part of the spectrometer firmware. It is a separate
// program, built by its own PlatformIO environment (see platformio.ini,
// [env:sd_test]), that does one job: prove the SD card wiring, the SPI
// port, and the FAT layer all work before any capture code depends on
// them. Flash it, open the serial monitor at 115200, read the report.
//
//   pio run -e sd_test -t upload
//   pio device monitor -e sd_test
//
// The card sits on SPI0. The ADC in main.cpp is bit-banged on GPIO 8-11
// and uses no SPI peripheral at all, so nothing here contends with it.
// The pins below are the RP2350 SPI0 defaults for this board, which is
// why no pin remap beyond the four setters in mountCard() is needed.

#include <Arduino.h>
#include <SPI.h>
#include <SdFat.h>
#include <string.h>

// Hardware configuration.

constexpr uint8_t PIN_SD_MISO = 16;  // INPUT:  card DAT0 / DO
constexpr uint8_t PIN_SD_CS = 17;    // OUTPUT: card chip select
constexpr uint8_t PIN_SD_SCLK = 18;  // OUTPUT: card CLK
constexpr uint8_t PIN_SD_MOSI = 19;  // OUTPUT: card CMD / DI

// Clock rates tried in order, fastest first.
//
// A card that fails at 24 MHz and mounts at 4 MHz is not a dead card, it
// is a wiring problem: long jumpers, no ground return beside the clock,
// or a breadboard. The test reports the rate that won, so that a later
// "it works" is not mistaken for "it works at full speed".
constexpr uint32_t CLOCK_TRY_MHZ[] = {24, 16, 8, 4, 1};

// Test parameters.

constexpr char SMALL_PATH[] = "sdtest.txt";
constexpr char BULK_PATH[] = "sdbulk.bin";

constexpr uint32_t BULK_BLOCK_BYTES = 512;  // one card sector
constexpr uint32_t BULK_BLOCK_COUNT = 128;  // 128 * 512 = 64 KiB
constexpr uint32_t BULK_TOTAL_BYTES = BULK_BLOCK_BYTES * BULK_BLOCK_COUNT;

SdFat sd;
static uint32_t g_clockMhz = 0;  // rate that mounted the card, 0 if none
static uint8_t g_buf[BULK_BLOCK_BYTES];

// Pattern byte for one absolute file offset.
//
// The value depends on the offset, not on the position inside the block.
// A card or a driver that returns the correct bytes in the wrong order,
// or that repeats a sector, fails this check. A constant fill would not
// catch either fault.
static inline uint8_t patternByte(uint32_t offset) {
  return (uint8_t)((offset * 31u + 7u) & 0xFFu);
}

static const char *cardTypeName(uint8_t type) {
  switch (type) {
    case SD_CARD_TYPE_SD1: return "SD1";
    case SD_CARD_TYPE_SD2: return "SD2";
    case SD_CARD_TYPE_SDHC: return "SDHC/SDXC";
    default: return "unknown";
  }
}

// Step 1: mount the card.

static bool mountCard() {
  SPI.setRX(PIN_SD_MISO);
  SPI.setCS(PIN_SD_CS);
  SPI.setSCK(PIN_SD_SCLK);
  SPI.setTX(PIN_SD_MOSI);

  for (uint32_t mhz : CLOCK_TRY_MHZ) {
    // DEDICATED_SPI, not SHARED_SPI: no other device is on SPI0, so SdFat
    // can keep the card selected between calls. That is worth roughly a
    // factor of two on multi-sector writes.
    SdSpiConfig cfg(PIN_SD_CS, DEDICATED_SPI, SD_SCK_MHZ(mhz), &SPI);
    if (sd.begin(cfg)) {
      g_clockMhz = mhz;
      Serial.printf("mount   OK at %lu MHz\n", (unsigned long)mhz);
      return true;
    }
    Serial.printf("mount   failed at %lu MHz\n", (unsigned long)mhz);
  }

  g_clockMhz = 0;
  Serial.println("mount   FAILED at every clock rate");
  Serial.print("        ");
  sd.initErrorPrint(&Serial);
  Serial.println("        Check: card inserted, 3V3 present, CS on GPIO 17,");
  Serial.println("        SCLK 18, MOSI 19, MISO 16, and a shared ground.");
  return false;
}

static void printCardInfo() {
  const uint64_t sectors = (uint64_t)sd.card()->sectorCount();
  const uint32_t mib = (uint32_t)(sectors / 2048);  // 512 B sectors to MiB

  Serial.printf("card    type %s, %lu sectors, %lu MiB\n",
                cardTypeName(sd.card()->type()),
                (unsigned long)sectors, (unsigned long)mib);
  Serial.printf("volume  FAT%d, SPI clock %lu MHz\n", (int)sd.fatType(),
                (unsigned long)g_clockMhz);
}

// Step 2: a small text file, written and then read back line by line.

static bool testSmallFile() {
  static const char *const LINES[] = {
      "sd bring-up test",
      "line two",
      "0123456789abcdef",
  };
  constexpr size_t LINE_COUNT = sizeof(LINES) / sizeof(LINES[0]);

  sd.remove(SMALL_PATH);  // a failure is expected if the file is absent

  FsFile f = sd.open(SMALL_PATH, O_WRONLY | O_CREAT | O_TRUNC);
  if (!f) {
    Serial.printf("small   FAILED: cannot create %s\n", SMALL_PATH);
    return false;
  }
  for (size_t i = 0; i < LINE_COUNT; i++) {
    f.println(LINES[i]);
  }
  // sync() before close() is deliberate. A failed flush is the last point
  // at which a full or write-protected card reports itself. To discard
  // that status turns a data loss into a silent pass.
  if (!f.sync()) {
    Serial.println("small   FAILED: sync error after write");
    f.close();
    return false;
  }
  f.close();

  f = sd.open(SMALL_PATH, O_RDONLY);
  if (!f) {
    Serial.println("small   FAILED: cannot open the file again to read");
    return false;
  }

  char line[64];
  bool ok = true;
  for (size_t i = 0; i < LINE_COUNT; i++) {
    const int n = f.fgets(line, sizeof(line));
    if (n <= 0) {
      Serial.printf("small   FAILED: file ends at line %u\n", (unsigned)i);
      ok = false;
      break;
    }
    // fgets keeps the newline. Remove it before the comparison.
    if (line[n - 1] == '\n') {
      line[n - 1] = '\0';
    }
    if (strcmp(line, LINES[i]) != 0) {
      Serial.printf("small   FAILED: line %u reads \"%s\", wrote \"%s\"\n",
                    (unsigned)i, line, LINES[i]);
      ok = false;
      break;
    }
  }
  f.close();

  if (ok) {
    Serial.printf("small   OK: %u lines written and read back\n",
                  (unsigned)LINE_COUNT);
  }
  return ok;
}

// Step 3: 64 KiB written and read, verified byte for byte and timed.
//
// Throughput is part of the result, not only correctness. Capture writes
// frames at the frame rate, so a card that verifies at 30 kB/s fails for
// the intended use even though every byte comes back correct.

static bool testBulkFile() {
  sd.remove(BULK_PATH);

  FsFile f = sd.open(BULK_PATH, O_WRONLY | O_CREAT | O_TRUNC);
  if (!f) {
    Serial.printf("bulk    FAILED: cannot create %s\n", BULK_PATH);
    return false;
  }
  // Preallocation keeps the FAT updates out of the timed section, which
  // is what the capture path will also do.
  if (!f.preAllocate(BULK_TOTAL_BYTES)) {
    Serial.println("bulk    note: preAllocate refused, timing includes FAT work");
  }

  const uint32_t writeStartUs = micros();
  for (uint32_t b = 0; b < BULK_BLOCK_COUNT; b++) {
    const uint32_t base = b * BULK_BLOCK_BYTES;
    for (uint32_t i = 0; i < BULK_BLOCK_BYTES; i++) {
      g_buf[i] = patternByte(base + i);
    }
    if (f.write(g_buf, BULK_BLOCK_BYTES) != (int)BULK_BLOCK_BYTES) {
      Serial.printf("bulk    FAILED: short write at block %lu\n",
                    (unsigned long)b);
      f.close();
      return false;
    }
  }
  if (!f.sync()) {
    Serial.println("bulk    FAILED: sync error after write");
    f.close();
    return false;
  }
  const uint32_t writeUs = micros() - writeStartUs;
  f.close();

  f = sd.open(BULK_PATH, O_RDONLY);
  if (!f) {
    Serial.println("bulk    FAILED: cannot open the file again to read");
    return false;
  }
  if (f.fileSize() != BULK_TOTAL_BYTES) {
    Serial.printf("bulk    FAILED: size %lu bytes, expected %lu\n",
                  (unsigned long)f.fileSize(),
                  (unsigned long)BULK_TOTAL_BYTES);
    f.close();
    return false;
  }

  const uint32_t readStartUs = micros();
  for (uint32_t b = 0; b < BULK_BLOCK_COUNT; b++) {
    const uint32_t base = b * BULK_BLOCK_BYTES;
    if (f.read(g_buf, BULK_BLOCK_BYTES) != (int)BULK_BLOCK_BYTES) {
      Serial.printf("bulk    FAILED: short read at block %lu\n",
                    (unsigned long)b);
      f.close();
      return false;
    }
    for (uint32_t i = 0; i < BULK_BLOCK_BYTES; i++) {
      if (g_buf[i] != patternByte(base + i)) {
        Serial.printf("bulk    FAILED: byte %lu reads 0x%02X, wrote 0x%02X\n",
                      (unsigned long)(base + i), g_buf[i],
                      patternByte(base + i));
        f.close();
        return false;
      }
    }
  }
  const uint32_t readUs = micros() - readStartUs;
  f.close();

  // kB/s from bytes and microseconds, in integer arithmetic. bytes * 1000
  // stays inside 32 bits for every size this test uses.
  const uint32_t writeKbs = writeUs ? (BULK_TOTAL_BYTES * 1000UL) / writeUs : 0;
  const uint32_t readKbs = readUs ? (BULK_TOTAL_BYTES * 1000UL) / readUs : 0;

  Serial.printf("bulk    OK: %lu bytes verified\n",
                (unsigned long)BULK_TOTAL_BYTES);
  Serial.printf("        write %lu us (%lu kB/s), read %lu us (%lu kB/s)\n",
                (unsigned long)writeUs, (unsigned long)writeKbs,
                (unsigned long)readUs, (unsigned long)readKbs);

  sd.remove(BULK_PATH);  // do not leave 64 KiB behind after every run
  return true;
}

// Step 4: a directory listing. It shows that the whole volume is
// readable, not only the handles that this test made itself.

static void listRoot() {
  Serial.println("root    listing:");
  FsFile dir = sd.open("/", O_RDONLY);
  if (!dir) {
    Serial.println("        FAILED: cannot open the root directory");
    return;
  }
  FsFile entry;
  char name[64];
  uint32_t count = 0;
  while (entry.openNext(&dir, O_RDONLY)) {
    entry.getName(name, sizeof(name));
    if (entry.isDir()) {
      Serial.printf("        %-32s <dir>\n", name);
    } else {
      Serial.printf("        %-32s %lu bytes\n", name,
                    (unsigned long)entry.fileSize());
    }
    entry.close();
    count++;
  }
  dir.close();
  Serial.printf("        %lu entries\n", (unsigned long)count);
}

static void runAllTests() {
  Serial.println();
  Serial.println("=== SD card test ===");
  Serial.printf("pins    MISO %u, CS %u, SCLK %u, MOSI %u (SPI0)\n",
                PIN_SD_MISO, PIN_SD_CS, PIN_SD_SCLK, PIN_SD_MOSI);

  if (!mountCard()) {
    Serial.println("RESULT  FAIL (no card)");
    Serial.println("Send r to run the test again.");
    return;
  }

  printCardInfo();

  bool ok = true;
  ok &= testSmallFile();
  ok &= testBulkFile();
  listRoot();

  Serial.println(ok ? "RESULT  PASS" : "RESULT  FAIL");
  Serial.println("Send r to run the test again.");
}

void setup() {
  Serial.begin(115200);

  // Wait for the host to open the port, but not for an unlimited time.
  // The board must still run when a wall supply powers it and no host is
  // connected.
  const uint32_t startMs = millis();
  while (!Serial && (millis() - startMs) < 3000) {
    delay(10);
  }

  runAllTests();
}

void loop() {
  if (Serial.available()) {
    const int c = Serial.read();
    if (c == 'r' || c == 'R') {
      sd.end();
      runAllTests();
    }
  }
  delay(10);
}
