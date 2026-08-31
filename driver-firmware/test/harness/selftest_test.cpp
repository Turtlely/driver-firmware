// Runs the firmware's boot self test against a simulated ADS7279.
//
// selftest_extract.inc is cut out of src/main.cpp by test/run_tests.py, so
// this exercises the shipping code rather than a copy of it. What is stubbed
// is everything below the test: the bit-banged transfer, the delay and the
// output queue.
//
// The point is the FAULT CASES. A self test that passes on a working part
// tells you nothing; what matters is that it fails on each way the SPI link
// is known to break, and that it says which one. An SDI line stuck high is
// the dangerous case, because the part answers command 1111b with plausible
// conversion data and the instrument keeps producing normal-looking spectra.

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

// --------------------------------------------------------------- the stubs

enum FaultMode {
  FAULT_NONE,        // a working part
  FAULT_SDI_HIGH,    // SDI stuck high: every command reads as 1111b
  FAULT_SDI_LOW,     // SDI stuck low: every command reads as 0000b
  FAULT_NO_ADC,      // nothing on the bus; SDO floats high
  FAULT_NO_RESET,    // SPI fine, but the part ignores the reset bit
};

static FaultMode g_fault = FAULT_NONE;

// The ADC command words and CFR masks are NOT declared here. They come out
// of src/main.cpp with the functions, in selftest_extract.inc, so a constant
// changed in the firmware cannot leave this harness testing the old value.
// That means the simulated part below has to be defined after the include,
// which is why adcCommand() is only declared at this point.
uint16_t adcCommand(uint16_t txWord);

static std::vector<std::string> g_lines;

bool txLine(const char *text) {
  g_lines.push_back(text);
  return true;
}

void delayMicroseconds(unsigned) {}

#include "selftest_extract.inc"

// One simulated ADS7279.
//
// Defined after the include, because it is built from the ADC constants the
// include carries. The register reads back what was written, masked by the
// bits the part does not implement. A reset returns it to the defaults and
// makes the NEXT transfer report all ones, which is the datasheet behaviour
// the firmware relies on.
struct FakeAdc {
  uint16_t cfr = ADC_CFR_DEFAULT;
  bool postReset = false;
  uint16_t conversion = 0x1234 & ~0x3;  // 14 bits then the 00 pad

  uint16_t transfer(uint16_t txWord) {
    if (postReset) {
      postReset = false;
      // The command still takes effect; only the returned word is all ones.
      apply(txWord);
      return ADC_POST_RESET_WORD;
    }
    return apply(txWord);
  }

  uint16_t apply(uint16_t txWord) {
    const uint16_t command = txWord & 0xF000;
    const uint16_t data = txWord & ADC_CFR_MASK;
    switch (command) {
      case ADC_CMD_WRITE_CFR:
        if (g_fault != FAULT_NO_RESET && (data & 1u) == 0u) {
          cfr = ADC_CFR_DEFAULT;   // D0 low is the software reset
          postReset = true;
          return conversion;
        }
        cfr = (uint16_t)(data & ~ADC_CFR_IGNORED);
        return conversion;
      case ADC_CMD_READ_CFR:
        // A CFR read replaces the low 12 bits with the register, and D0
        // reads 1 in normal operation.
        return (uint16_t)((conversion & 0xF000) | cfr | 0x1);
      default:
        // Every other command, 1111b included, returns conversion data.
        return conversion;
    }
  }
};

static FakeAdc g_adc;

uint16_t adcCommand(uint16_t txWord) {
  switch (g_fault) {
    case FAULT_SDI_HIGH:
      // The part sees 1111b whatever was sent: default mode, conversion data.
      return g_adc.apply(0xF000);
    case FAULT_SDI_LOW:
      return g_adc.apply(0x0000);
    case FAULT_NO_ADC:
      return 0xFFFF;   // an undriven bus reads high
    default:
      return g_adc.transfer(txWord);
  }
}

// --------------------------------------------------------------- the check

static int g_failures = 0;

static void expect(bool ok, const char *what) {
  if (!ok) {
    printf("    FAIL: %s\n", what);
    ++g_failures;
  }
}

static bool sawLine(const char *needle) {
  for (const std::string &line : g_lines) {
    if (line.find(needle) != std::string::npos) return true;
  }
  return false;
}

static bool run(FaultMode fault, const char *name) {
  g_fault = fault;
  g_adc = FakeAdc();
  g_lines.clear();
  const bool pass = runSelfTest();
  printf("-- %s\n", name);
  for (const std::string &line : g_lines) {
    printf("   %s\n", line.c_str());
  }
  return pass;
}

int main() {
  // A working part must pass, and must leave the CFR at its defaults: that
  // is the state the first pixel read wants.
  expect(run(FAULT_NONE, "a working part"), "a working part must pass");
  expect(g_adc.cfr == ADC_CFR_DEFAULT,
         "the sequence must leave the CFR at its defaults");
  expect(!g_adc.postReset,
         "the all-ones cycle after the reset must be consumed, or the first "
         "pixel reads full scale");
  expect(sawLine("selftest=PASS"), "the summary must say PASS");
  expect(g_selfTestPass, "the flag the I line reports must be set");

  // SDI stuck high is the dangerous fault: the part answers command 1111b
  // with plausible conversion data, so nothing downstream can see it.
  expect(!run(FAULT_SDI_HIGH, "SDI stuck high"),
         "SDI stuck high must fail");
  expect(sawLine("cmdfield") && sawLine("FAIL"),
         "SDI stuck high must fail the command field test");
  expect(sawLine("selftest=FAIL"), "the summary must say FAIL");
  expect(!g_selfTestPass, "the flag the I line reports must be clear");

  expect(!run(FAULT_SDI_LOW, "SDI stuck low"), "SDI stuck low must fail");
  expect(sawLine("selftest=FAIL"), "the summary must say FAIL");

  // No part on the bus at all. This must report, not hang: nothing in the
  // sequence waits on an external signal.
  expect(!run(FAULT_NO_ADC, "no ADC on the bus"),
         "an absent ADC must fail");
  expect(sawLine("selftest=FAIL"), "the summary must say FAIL");

  // SPI works and the part ignores the reset. The self check must pass and
  // the reset must fail, so the report names the right half.
  expect(!run(FAULT_NO_RESET, "SPI fine, reset ignored"),
         "an ignored reset must fail");
  expect(sawLine("sditest=PASS"),
         "the SPI half must still pass when only the reset is broken");
  expect(sawLine("adcreset=FAIL"), "the reset half must fail");
  expect(sawLine("selftest=FAIL spi=PASS adcreset=FAIL"),
         "the summary must say which half failed");

  if (g_failures) {
    printf("\nselftest harness: %d expectation(s) failed\n", g_failures);
    return 1;
  }
  printf("\nboot self test ok: passes on a working part, fails on each "
         "broken link, and names which half\n");
  return 0;
}
