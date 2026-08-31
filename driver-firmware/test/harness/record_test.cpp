// Emits one stacked-capture result record on stdout, using the firmware's own
// buildStackHeader() and buildStackPixels().
//
// Those functions are not here. test/run_tests.py extracts them from
// src/main.cpp before every build, so the host parser is tested against the
// bytes the device really sends rather than against a second description of
// the format.
//
// Contents are fixed, and record_check.py knows what to expect.

#include <cstdio>
#include <cstdint>
#include <cstring>
#include <cmath>

#ifdef _WIN32
#include <fcntl.h>
#include <io.h>
#endif

constexpr uint32_t PIXEL_COUNT = 1024;

struct {
  uint32_t id;
  uint32_t framesRequested;
  uint32_t framesAttempted;
  uint32_t framesStacked;
  uint32_t framesExcluded;
  uint32_t exposureUs;
  uint32_t durationMs;
  uint32_t blockFrames;
  uint32_t sigmaX100;
  uint32_t clipIters;
  uint32_t statMissing;
  uint32_t statStale;
  uint32_t statBadBits;
  uint32_t statMaxLateUs;
  uint32_t statNoStart;
  bool dark;
} capture;

struct {
  uint32_t sum[PIXEL_COUNT];
  uint64_t sumSq[PIXEL_COUNT];
  uint16_t accepted[PIXEL_COUNT];
  uint16_t rejected[PIXEL_COUNT];
} g_stack;

#include "record_extract.inc"

int main() {
#ifdef _WIN32
  _setmode(_fileno(stdout), _O_BINARY);
#endif
  capture.id = 7;
  capture.dark = true;
  capture.framesRequested = 150;
  capture.framesAttempted = 150;
  capture.framesStacked = 148;
  capture.framesExcluded = 2;
  capture.exposureUs = 66666;
  capture.durationMs = 10012;
  capture.blockFrames = 16;
  capture.sigmaX100 = 300;
  capture.clipIters = 5;
  capture.statMissing = 41;
  capture.statStale = 9;
  capture.statBadBits = 3;
  capture.statMaxLateUs = 812;
  capture.statNoStart = 1;

  // Pixel p holds (p mod 148)+1 samples all equal to (p*7) mod 16384, so its
  // mean is that value exactly and its deviation is zero. Pixel 3 gets no
  // samples at all, which is the NaN path.
  for (uint32_t p = 0; p < PIXEL_COUNT; ++p) {
    if (p == 3) continue;
    const uint32_t n = (p % 148) + 1;
    const uint32_t val = (p * 7u) % 16384u;
    g_stack.sum[p] = n * val;
    g_stack.sumSq[p] = (uint64_t)n * (uint64_t)val * (uint64_t)val;
    g_stack.accepted[p] = (uint16_t)n;
    g_stack.rejected[p] = (uint16_t)(p % 5);
  }

  // Same chunking pumpResult() uses, so a chunk boundary fault would show.
  uint8_t buf[STACK_PIXELS_PER_CHUNK * STACK_PIXEL_BYTES];
  uint32_t checksum = 0;

  size_t len = buildStackHeader(buf);
  for (size_t i = 0; i < len; ++i) checksum += buf[i];
  std::fwrite(buf, 1, len, stdout);

  for (uint32_t px = 0; px < PIXEL_COUNT; px += STACK_PIXELS_PER_CHUNK) {
    len = buildStackPixels(buf, px, STACK_PIXELS_PER_CHUNK);
    for (size_t i = 0; i < len; ++i) checksum += buf[i];
    std::fwrite(buf, 1, len, stdout);
  }

  putU32(buf, checksum);
  std::fwrite(buf, 1, 4, stdout);
  std::fflush(stdout);

  std::fprintf(stderr, "%u\n", (unsigned)STACK_RECORD_BYTES);
  return 0;
}
