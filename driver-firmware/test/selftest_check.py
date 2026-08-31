"""Runs the boot self test harness and reports what it found.

The work is in test/harness/selftest_test.cpp, which compiles the firmware's
own self-test functions against a simulated ADS7279. This wrapper runs it and
also checks the host parses the selftest field the "I " line now carries.
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "tools"))
from spectrometer.device import parse_info  # noqa: E402

EXE = HERE / "build" / ("selftest_test.exe" if sys.platform == "win32"
                        else "selftest_test")

result = subprocess.run([str(EXE)], capture_output=True, text=True)
print(result.stdout, end="")
if result.stderr:
    print(result.stderr, end="", file=sys.stderr)
if result.returncode != 0:
    sys.exit(1)

# The host has to be able to read the outcome. The boot lines scroll past
# long before anyone connects, so the "I " line is where the result is
# actually read from.
INFO = ("I fw=1.4.0 proto=3 board=rpipico2 pixels=1024 bits=14 vref_mv=2500 "
        "rate_hz=15 rate_min=1 rate_max=60 pixel_period_us=20 frame_us=66666 "
        "frame_us_min=20600 frame_us_max=10000000 refractory_us=5 "
        "sysclk_mhz=150 pio_crc=0x1 missing_code=0xFFFF stack=1 "
        "stack_block=16 stack_block_max=32 stack_sigma_x100=300 "
        "cap_max_frames=16384 cap_max_s=600 mode=0 sd=1 cal=1 cal_n=0 "
        "cal_store=1 selftest=1 built=Aug_30_2026")
info = parse_info(INFO)
assert info.selftest == 1, info.selftest
assert info.self_test_passed, "selftest=1 must read as a pass"

failed = parse_info(INFO.replace("selftest=1", "selftest=0"))
assert not failed.self_test_passed, "selftest=0 must read as a failure"

# An older firmware sends no selftest field at all. That is unknown, not a
# failure, and must not be reported as one.
old = parse_info(INFO.replace("selftest=1 ", ""))
assert old.selftest == -1, old.selftest
assert old.self_test_passed is None, "an absent field is unknown, not failed"

print("selftest check ok")
