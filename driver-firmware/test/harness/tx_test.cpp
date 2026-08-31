// Runs the firmware's transmit queue and result sender on this computer,
// against a fake serial port, while diagnostic text tries to get out at the
// same time.
//
// This exists because of a real failure. reportFilteredTriggerCount() writes
// about 110 bytes on every frame in every mode, and the result record is fed
// out in 384 byte chunks over many loop() passes. The text landed inside the
// record, the host read a record of the wrong length, and the capture hung at
// "collecting result". Nothing in the earlier checks could see it, because
// they fed the record to the host directly instead of through the queue that
// both writers share.
//
// The queue code, the sender and the record builders are all extracted from
// src/main.cpp by test/run_tests.py. Only the port and the clock are fake.
//
// Writes the bytes the port received to stdout. tx_interleave_check.py parses
// them.

#include <cstdio>
#include <cstdint>
#include <cstring>
#include <cmath>
#include <vector>

#ifdef _WIN32
#include <fcntl.h>
#include <io.h>
#endif

constexpr uint32_t PIXEL_COUNT = 1024;

// What runFrame() reserves for one binary frame plus its status line.
constexpr size_t FRAME_STANDIN_BYTES =
    (4 + 4 + 2 + 2 + 2 * PIXEL_COUNT + 2) + 448;

// A USB CDC port that accepts a little at a time, which is what forces the
// sender to spread a record across many passes in the first place.
static std::vector<uint8_t> g_wire;
struct FakeSerial {
  int availableForWrite() { return 64; }
  size_t write(const uint8_t *data, size_t len) {
    g_wire.insert(g_wire.end(), data, data + len);
    return len;
  }
} Serial;

static uint32_t g_nowUs = 0;
static uint32_t micros() { return g_nowUs; }

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

#include "tx_extract.inc"
#include "record_extract.inc"
#include "sender_extract.inc"

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

  for (uint32_t p = 0; p < PIXEL_COUNT; ++p) {
    if (p == 3) continue;
    const uint32_t n = (p % 148) + 1;
    const uint32_t val = (p * 7u) % 16384u;
    g_stack.sum[p] = n * val;
    g_stack.sumSq[p] = (uint64_t)n * (uint64_t)val * (uint64_t)val;
    g_stack.accepted[p] = (uint16_t)n;
    g_stack.rejected[p] = (uint16_t)(p % 5);
  }

  // A stand-in for the binary frame runFrame() stages. Its content does not
  // matter; that it is offered through txFrameReady() does. The frame path is
  // the writer that reading a file off the card exposed: a capture leaves the
  // device QUIET and stages no frames, so only the text path was ever tested
  // here, and a bare txFree() check in runFrame() let every frame during an
  // "sd get" land inside the record.
  static uint8_t frameBuf[FRAME_STANDIN_BYTES];
  memset(frameBuf, 'F', sizeof(frameBuf));
  memcpy(frameBuf, "FRAMEBEG", 8);

  // Before the transfer: ordinary text and a frame, which must both get out.
  // Staging one here is what keeps the "none inside" check below from being
  // vacuous: it proves the harness can stage a frame at all.
  txLine("A cap sending id=7 bytes=12356");
  int framesBefore = 0;
  if (txFrameReady(sizeof(frameBuf))) {
    txAppend(frameBuf, sizeof(frameBuf));
    ++framesBefore;
  }
  for (int i = 0; i < 200; ++i) pumpTx();

  beginResultSend();

  // Now the transfer, with a frame arriving every so often, writing its
  // diagnostic line and staging its pixels, exactly as loop() and runFrame()
  // do.
  int frames = 0;
  int framesStaged = 0;
  for (int pass = 0; pass < 200000 && resultSender.active; ++pass) {
    g_nowUs += 50;
    pumpTx();
    pumpResult();
    if ((pass % 3) == 0) {   // far more often than a real frame, on purpose
      ++frames;
      txLine("T frame 123 filtered 1024 expected 1024 delta +0 "
             "extrafirst 0us extragap 0-0us");
      if (txFrameReady(sizeof(frameBuf))) {
        txAppend(frameBuf, sizeof(frameBuf));
        ++framesStaged;
      }
    }
  }
  while (txPending() > 0) pumpTx();

  // After the transfer: text must get through again, or the lock leaked.
  txLine("A live=1");
  while (txPending() > 0) pumpTx();

  std::fwrite(g_wire.data(), 1, g_wire.size(), stdout);
  std::fflush(stdout);
  std::fprintf(stderr,
               "sent=%zu frames_during_transfer=%d staged_before=%d "
               "staged_during=%d active=%d\n",
               g_wire.size(), frames, framesBefore, framesStaged,
               (int)resultSender.active);
  return resultSender.active ? 1 : 0;
}
