# driver-firmware

Firmware and host software for a line-scan spectrometer: a Raspberry Pi
Pico 2 (RP2350), an ADS7279 14-bit ADC, and a CPLD driver board operating a
Hamamatsu S3903-1024Q linear photodiode array.

## System overview

The Pico does not generate sensor timing. The CPLD does. The Pico starts a
frame, captures the 1024 pixel values the CPLD produces, and streams them
to the host over USB serial.

One frame:

1. The Pico asserts a START pulse to the CPLD.
2. The CPLD scans the sensor one pixel at a time.
3. For each pixel it drives the sensor and triggers an ADS7279 conversion.
4. The ADS7279 asserts `~INT` when the result is ready.
5. `~INT` is wired to a Pico GPIO. Each falling edge means one pixel is
   ready, and the Pico reads the 16-bit result over SPI.
6. After 1024 pixels the frame is transmitted and the next one starts.

`~INT` comes from the ADS7279 itself, not from the CPLD as a separate
marker, so no fixed settling delay is inserted before the read.

## Running the application

```
python tools/spectrometer_app.py
```

Requires `pyserial` and `numpy`. Connect, and the device reports its own
configuration. Supports live view, frame averaging, fixed-count capture
with outlier rejection, dark and flat correction, wavelength calibration,
peak detection, statistics and spectrum export.

See `driver-firmware/docs/APPLICATION.md`.

## Building the firmware

```
cd driver-firmware
pio run -t upload
```

PIO headers are regenerated automatically before each build. If upload
fails because picotool cannot claim the device, put the board in BOOTSEL
and copy `.pio/build/pico2/firmware.uf2` to the mass-storage drive it
presents.

## Documentation

| Document | Contents |
|---|---|
| `driver-firmware/docs/ARCHITECTURE.md` | Firmware design, timing, wire protocol |
| `driver-firmware/docs/APPLICATION.md` | Operating the application, host code design |
| `driver-firmware/docs/ISSUES.md` | Every defect found, its cause and resolution |

`ISSUES.md` is the right starting point before modifying the capture path.
Several obvious-looking simplifications have already been tried and have
already failed.

## Repository layout

```
driver-firmware/
  src/main.cpp              firmware
  src/pixel_trig.pio        PIO trigger debounce
  tools/spectrometer_app.py application launcher
  tools/spectrometer/       application modules
  tools/build_pio.py        pre-build PIO header regeneration
  tools/live_view.py        minimal development viewer
  docs/                     documentation
```

## Hardware notes

- The Pico runs at 3.3 V. The ADS7279 sits in a separate domain behind an
  external level shifter.
- SPI is bit-banged on GPIO, not driven by the hardware peripheral, so the
  clock can be generated on demand per trigger rather than free-running.
- The SPI read path is placed in RAM. Executing it from flash introduced
  timing variability that corrupted reads (`ISSUES.md` entry 21).

## Status

Capture is stable: 1024 pixels per frame with no measured loss or
corruption over extended runs. Known open items, all analog or diagnostic
rather than capture integrity, are listed at the end of `ISSUES.md`.
