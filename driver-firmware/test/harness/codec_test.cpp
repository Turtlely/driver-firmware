// Runs the firmware's card codec on this computer.
//
// The coder and the decoder are extracted from src/main.cpp by
// test/run_tests.py, so this either tests the firmware that is in the tree or
// it does not build at all. Only the input and the reporting are local.
//
// Two modes:
//
//   codec_test              read 12356 byte records on stdin, encode each,
//                           decode it again, and fail if the round trip is
//                           not exact. Writes <uint32 length><payload> per
//                           record to stdout so that codec_check.py can
//                           decode them with an independent implementation.
//
//   codec_test decode       read one <uint32 length><payload> from stdin and
//                           run only the decoder. Prints the number of pixel
//                           bytes it produced, which is 0 when it rejects the
//                           input. This is the mode that feeds it damaged
//                           payloads.

#include <cstdio>
#include <cstdint>
#include <cstring>
#include <cstdlib>
#include <vector>

#ifdef _WIN32
#include <fcntl.h>
#include <io.h>
#endif

constexpr uint32_t PIXEL_COUNT = 1024;
constexpr size_t STACK_PIXEL_BYTES = 12;
constexpr size_t STACK_HEADER_BYTES = 64;
constexpr size_t STACK_RECORD_BYTES =
    STACK_HEADER_BYTES + STACK_PIXEL_BYTES * PIXEL_COUNT + 4;

// The two byte writers the container header uses. Four lines each, and the
// field ORDER is what matters here, which comes from the extract below.
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

#include "codec_extract.inc"

static std::vector<uint8_t> readAll() {
  std::vector<uint8_t> data;
  uint8_t chunk[4096];
  size_t n;
  while ((n = std::fread(chunk, 1, sizeof(chunk), stdin)) > 0) {
    data.insert(data.end(), chunk, chunk + n);
  }
  return data;
}

int main(int argc, char **argv) {
#ifdef _WIN32
  _setmode(_fileno(stdin), _O_BINARY);
  _setmode(_fileno(stdout), _O_BINARY);
#endif
  std::vector<uint8_t> in = readAll();

  if (argc > 1 && std::strcmp(argv[1], "file") == 0) {
    // One whole card file, assembled the way sdSaveResult() assembles it and
    // with the same header builder. This is what tools/read_stk.py is tested
    // against, so a card reader is proven able to read what the firmware
    // writes rather than what a second encoder in the test says it writes.
    if (in.size() != STACK_RECORD_BYTES) {
      std::fprintf(stderr, "expected one record\n");
      return 2;
    }
    static uint8_t payload[SD_PAYLOAD_MAX];
    const uint8_t *pixels = in.data() + STACK_HEADER_BYTES;
    uint16_t codec = SD_CODEC_RLE;
    size_t n = sdEncodePixels(pixels, PIXEL_COUNT, STACK_PIXEL_BYTES, payload,
                              sizeof(payload));
    const uint8_t *body = payload;
    if (n == 0 || n >= SD_PIXEL_BYTES) {
      codec = SD_CODEC_NONE;
      body = pixels;
      n = SD_PIXEL_BYTES;
    }
    uint32_t sum = 0;
    for (size_t i = 0; i < n; ++i) sum += body[i];

    uint8_t head[SD_FILE_HEADER_BYTES];
    const size_t hn = sdBuildFileHeader(head, codec, (uint32_t)n, sum);
    std::fwrite(head, 1, hn, stdout);
    std::fwrite(in.data(), 1, STACK_HEADER_BYTES, stdout);
    std::fwrite(body, 1, n, stdout);
    std::fwrite(in.data() + STACK_RECORD_BYTES - 4, 1, 4, stdout);
    std::fflush(stdout);
    return 0;
  }

  if (argc > 1 && std::strcmp(argv[1], "decode") == 0) {
    if (in.size() < 4) {
      std::fprintf(stderr, "no payload\n");
      return 2;
    }
    const uint32_t len = (uint32_t)in[0] | ((uint32_t)in[1] << 8) |
                         ((uint32_t)in[2] << 16) | ((uint32_t)in[3] << 24);
    if (in.size() < 4 + len) {
      std::fprintf(stderr, "payload shorter than its length field\n");
      return 2;
    }
    // Poisoned, so an overrun writes somewhere detectable rather than into
    // whatever happened to be next.
    static uint8_t pixels[STACK_PIXEL_BYTES * PIXEL_COUNT];
    std::memset(pixels, 0xCD, sizeof(pixels));
    const size_t got = sdDecodePixels(in.data() + 4, len, pixels, PIXEL_COUNT,
                                      STACK_PIXEL_BYTES);
    std::printf("%zu\n", got);
    return 0;
  }

  if (in.size() % STACK_RECORD_BYTES != 0 || in.empty()) {
    std::fprintf(stderr, "expected whole records of %zu bytes, got %zu\n",
                 STACK_RECORD_BYTES, in.size());
    return 2;
  }

  static uint8_t payload[SD_PAYLOAD_MAX];
  static uint8_t back[STACK_PIXEL_BYTES * PIXEL_COUNT];
  const size_t records = in.size() / STACK_RECORD_BYTES;
  size_t plainTotal = 0;
  size_t storedTotal = 0;

  for (size_t r = 0; r < records; ++r) {
    const uint8_t *rec = in.data() + r * STACK_RECORD_BYTES;
    const uint8_t *pixels = rec + STACK_HEADER_BYTES;

    const size_t n = sdEncodePixels(pixels, PIXEL_COUNT, STACK_PIXEL_BYTES,
                                    payload, sizeof(payload));
    if (n == 0) {
      std::fprintf(stderr, "record %zu: the coder refused its own buffer\n", r);
      return 1;
    }

    std::memset(back, 0xCD, sizeof(back));
    const size_t got =
        sdDecodePixels(payload, n, back, PIXEL_COUNT, STACK_PIXEL_BYTES);
    if (got != SD_PIXEL_BYTES) {
      std::fprintf(stderr, "record %zu: decode returned %zu\n", r, got);
      return 1;
    }
    if (std::memcmp(back, pixels, SD_PIXEL_BYTES) != 0) {
      std::fprintf(stderr, "record %zu: the round trip changed the data\n", r);
      return 1;
    }

    const uint8_t len[4] = {(uint8_t)(n & 0xFF), (uint8_t)((n >> 8) & 0xFF),
                            (uint8_t)((n >> 16) & 0xFF),
                            (uint8_t)((n >> 24) & 0xFF)};
    std::fwrite(len, 1, 4, stdout);
    std::fwrite(payload, 1, n, stdout);

    plainTotal += SD_PIXEL_BYTES;
    storedTotal += n;
  }
  std::fflush(stdout);

  std::fprintf(stderr, "records=%zu pixel_bytes=%zu stored=%zu ratio=%.3f\n",
               records, plainTotal, storedTotal,
               (double)plainTotal / (double)storedTotal);
  return 0;
}
