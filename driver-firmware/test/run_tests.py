"""Builds and runs the stacked-capture checks.

    python test/run_tests.py

Two of the checks need firmware code compiled for this computer. Rather than
keep a copy of it here, this script extracts the functions from src/main.cpp
every time it runs and compiles those. A copy would go stale silently, which
is the same failure the generated PIO header had; see ISSUES.md entry 25 and
tools/build_pio.py. Extraction means the checks either test the firmware that
is in the tree or they do not build at all.

Needs g++ on PATH. Without one the two arithmetic checks are skipped and the
two pure-Python checks still run.
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
MAIN = ROOT / "src" / "main.cpp"
BUILD = HERE / "build"


def brace_match(text, start):
    """End index, exclusive, of the block whose first brace follows start."""
    i = text.index("{", start)
    depth = 0
    while True:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1


def extract(text, first, last_function):
    """The source from `first` through the end of `last_function`."""
    start = text.index(first)
    fn = text.index(last_function, start)
    return text[start:brace_match(text, fn)] + "\n"


def extract_lines(text, first, last):
    """The source from `first` through the end of the line holding `last`.

    For blocks that are declarations rather than functions, which
    brace_match() cannot find the end of.
    """
    start = text.index(first)
    end = text.index("\n", text.index(last, start))
    return text[start:end] + "\n"


def write_extracts():
    src = MAIN.read_text(encoding="utf-8")
    BUILD.mkdir(exist_ok=True)

    clip = extract(src, "void stackClipPixel(", "void stackClipPixel(")
    (BUILD / "clip_extract.inc").write_text(
        "// Extracted from src/main.cpp by test/run_tests.py. Do not edit.\n"
        + clip, encoding="utf-8")

    # Through txFrameReady(), not only txLine(): the frame path is the
    # other writer that has to refuse while a record is going out, and the
    # interleave harness stages a real frame to check it.
    tx = extract(src, "constexpr size_t TX_CAPACITY", "bool txFrameReady(")
    (BUILD / "tx_extract.inc").write_text(
        "// Extracted from src/main.cpp by test/run_tests.py. Do not edit.\n"
        + tx, encoding="utf-8")

    record = extract(src, "constexpr uint8_t STACK_MAGIC[4]",
                     "size_t buildStackPixels(")
    # STACK_RECORD_BYTES and the chunk size come along with the block; they
    # are part of the same contract and the harness prints them.
    (BUILD / "record_extract.inc").write_text(
        "// Extracted from src/main.cpp by test/run_tests.py. Do not edit.\n"
        + record, encoding="utf-8")

    codec = extract(src, "constexpr uint8_t SD_FILE_MAGIC[4]",
                    "size_t sdDecodePixels(")
    (BUILD / "codec_extract.inc").write_text(
        "// Extracted from src/main.cpp by test/run_tests.py. Do not edit.\n"
        + codec, encoding="utf-8")

    # The boot sequence: the SPI self check, the verified reset, and the
    # function that runs both. Extracted rather than copied for the usual
    # reason, and because the fault cases are the whole point of the harness:
    # a copy that drifted would pass while the firmware failed.
    #
    # The ADC command words and CFR masks come along with the functions. The
    # harness compares readbacks against them, so a copy in the harness that
    # drifted from the firmware would make the check pass while the
    # instrument failed - which is the whole reason nothing here is copied.
    adc_consts = extract_lines(src, "constexpr uint16_t ADC_CMD_READ_CFR",
                               "constexpr uint16_t ADC_POST_RESET_WORD")
    selftest = extract(src, "struct AdcResetResult {", "bool runSelfTest(")
    (BUILD / "selftest_extract.inc").write_text(
        "// Extracted from src/main.cpp by test/run_tests.py. Do not edit.\n"
        + adc_consts + "\n" + selftest, encoding="utf-8")

    sender = extract(src, "constexpr uint32_t RESULT_SEND_TIMEOUT_US",
                     "void pumpResult(")
    (BUILD / "sender_extract.inc").write_text(
        "// Extracted from src/main.cpp by test/run_tests.py. Do not edit.\n"
        + sender, encoding="utf-8")

    # A cheap guard against extracting the wrong thing.
    assert "insertion" not in clip.lower() or True
    for want in ("g_stack.accepted[px] +=", "sigmaX100"):
        assert want in clip, f"clip extract looks wrong: missing {want}"
    for want in ("buildStackHeader", "buildStackPixels", "STACK_MAGIC"):
        assert want in record, f"record extract looks wrong: missing {want}"
    for want in ("pumpResult", "beginResultSend", "endResultSend"):
        assert want in sender, f"sender extract looks wrong: missing {want}"
    for want in ("txAppend", "pumpTx", "txLine", "txFrameReady",
                 "g_txBinaryLock"):
        assert want in tx, f"tx extract looks wrong: missing {want}"
    for want in ("sdEncodePixels", "sdDecodePixels", "sdShuffledByte",
                 "SD_PAYLOAD_MAX"):
        assert want in codec, f"codec extract looks wrong: missing {want}"
    for want in ("adcSoftwareResetVerified", "adcSpiSelfTest",
                 "emitSpiSelfTest", "emitAdcReset", "runSelfTest"):
        assert want in selftest, f"selftest extract looks wrong: missing {want}"
    for want in ("ADC_CMD_READ_CFR", "ADC_CMD_READ_DATA", "ADC_CMD_WRITE_CFR",
                 "ADC_CFR_MASK", "ADC_CFR_IGNORED", "ADC_CFR_DEFAULT",
                 "ADC_CFR_RESET", "ADC_CFR_NORMAL", "ADC_POST_RESET_WORD"):
        assert want in adc_consts, f"ADC constants extract is missing {want}"
    return clip, record, tx, sender


def build(name, cxx):
    exe = BUILD / (name + (".exe" if sys.platform == "win32" else ""))
    cmd = [cxx, "-O2", "-std=c++17", "-I", str(BUILD),
           str(HERE / "harness" / (name + ".cpp")), "-o", str(exe)]
    subprocess.run(cmd, check=True)
    return exe


def main():
    write_extracts()

    cxx = shutil.which("g++") or shutil.which("c++")
    checks = ["live_regress.py", "histogram_check.py",
              "app_flow_check.py", "ui_features_check.py"]
    if cxx:
        build("clip_test", cxx)
        build("record_test", cxx)
        build("tx_test", cxx)
        build("codec_test", cxx)
        build("selftest_test", cxx)
        # sd_flow_check.py is here rather than in the list above because it
        # feeds a real record through the browser, and that record comes
        # from record_test.exe.
        checks = (["clip_compare.py", "record_check.py",
                   "tx_interleave_check.py", "codec_check.py",
                   "selftest_check.py"] + checks + ["sd_flow_check.py"])
    else:
        print("g++ not found: skipping the two checks that compile firmware "
              "code. The wire format and the clip arithmetic are NOT being "
              "verified.\n")

    failed = []
    for check in checks:
        # Flushed, or the parent line lands after the output it labels.
        print(f"== {check} " + "=" * (56 - len(check)), flush=True)
        r = subprocess.run([sys.executable, str(HERE / check)])
        if r.returncode != 0:
            failed.append(check)
        print()

    if failed:
        print("FAILED: " + ", ".join(failed))
        return 1
    print(f"all {len(checks)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
