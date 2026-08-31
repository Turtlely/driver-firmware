// Runs the firmware's stackClipPixel() on this computer.
//
// The function itself is not here. test/run_tests.py extracts it from
// src/main.cpp before every build, so this harness cannot test a stale copy.
//
// Reads "sigma_x100 iters n" followed by n codes on stdin, and prints the
// clipped sum, the accepted count and the rejected count for each case.

#include <cstdio>
#include <cstdint>
#include <cstring>
#include <cmath>

// The declarations the extracted function needs, matching src/main.cpp.
constexpr uint32_t PIXEL_COUNT = 1;  // one pixel per case, so the block
                                     // stride is 1 and a case is a plain
                                     // list of samples
constexpr uint32_t STACK_BLOCK_FRAMES_MAX = 32;
constexpr uint16_t ADC_CODE_MASK = 0x3FFF;

struct {
  uint32_t sigmaX100;
  uint32_t clipIters;
} capture;

struct {
  uint32_t sum[PIXEL_COUNT];
  uint64_t sumSq[PIXEL_COUNT];
  uint16_t accepted[PIXEL_COUNT];
  uint16_t rejected[PIXEL_COUNT];
} g_stack;

#include "clip_extract.inc"

int main() {
  uint16_t block[STACK_BLOCK_FRAMES_MAX];
  unsigned sigma, iters, n;
  while (std::scanf("%u %u %u", &sigma, &iters, &n) == 3) {
    if (n > STACK_BLOCK_FRAMES_MAX) return 2;
    capture.sigmaX100 = sigma;
    capture.clipIters = iters;
    std::memset(&g_stack, 0, sizeof(g_stack));
    for (unsigned i = 0; i < n; ++i) {
      unsigned x;
      if (std::scanf("%u", &x) != 1) return 1;
      block[i] = (uint16_t)x;
    }
    stackClipPixel(block, n, 0);
    std::printf("%u %u %u\n", (unsigned)g_stack.sum[0],
                (unsigned)g_stack.accepted[0], (unsigned)g_stack.rejected[0]);
    std::fflush(stdout);
  }
  return 0;
}
