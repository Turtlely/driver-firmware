# driver-firmware

Firmware and host tools for a line-scan spectrometer. The instrument uses a
Raspberry Pi Pico 2 (RP2350), an ADS7279 14-bit ADC, and a CPLD driver board.
The sensor is a Hamamatsu S3903-1024Q linear photodiode array.

This page tells you how to use the tools. For the design, read
`driver-firmware/docs/ARCHITECTURE.md`.

## Before you start

Install Python 3, then install the two packages the tools need:

```
pip install pyserial numpy
```

Only one program can open the serial port. Close the application before you
run a script or a serial monitor.

## Run the application

```
python driver-firmware/tools/spectrometer_app.py
```

Select the port, then click **Connect**. The device reports its own
configuration.

The application gives you a live view, frame averaging, and capture of a fixed
number of frames. It also gives you dark and flat correction, wavelength
calibration, peak detection and spectrum export. For full instructions, read
`driver-firmware/docs/APPLICATION.md`.

To make a Windows desktop shortcut, run this script one time:

```
powershell -ExecutionPolicy Bypass -File driver-firmware/tools/make_desktop_shortcut.ps1
```

## Build and flash the firmware

```
cd driver-firmware
pio run -t upload
```

The build regenerates the PIO headers first. Do not run `pioasm` by hand.

If the upload fails because picotool cannot claim the device, flash the board
by hand. Hold BOOTSEL, connect the board, then copy
`.pio/build/pico2/firmware.uf2` to the drive that the board presents.

## Other tools

Each script needs the serial port. Close the application first.

| Command | Function |
|---|---|
| `python tools/live_view.py [port] [baud]` | Minimal viewer for development. Finds the port if you give no argument. |
| `python tools/measure_pixel_noise.py [pixel ...]` | Records the values of one or more pixels over time and reports the distribution. |
| `python tools/measure_rate_response.py` | Measures the effect of frame rate on signal and noise. Takes about two minutes. |

## Serial commands

Send one command for each line, at 115200 baud. The device answers with a line
that starts with `A `. An error starts with `E `.

| Command | Function |
|---|---|
| `id` | Reports the firmware version and all acquisition parameters. |
| `rate <hz>` | Sets the frame rate. Use `0` for free running. |
| `period <us>` | Sets the frame period in microseconds. This also sets the integration time. |

The commands below test the hardware. Normal operation does not need them.

| Command | Function |
|---|---|
| `pulsewidth` | Measures the low time of the pixel trigger pulse. |
| `pairs` | Reports the last read triples that did not agree. |
| `sclk <n>` | Sets the SPI clock half period. Use `0` for normal speed. A large value drops pixels. |
| `readdelay <us>` | Sets a settling delay before each read. Use `0` for normal operation. |
| `cfr` | Reads the ADC configuration register eight times. |
| `cfrw <hex>` | Writes the ADC configuration register, then reads it back. |
| `mosi <0\|1>` | Sets the idle level of the ADC data input. |
| `adcreset` | Sends a software reset to the ADC. |

**Warning:** `sclk`, `readdelay` and `mosi` change how the instrument
captures data. Set each one back to `0` when you finish. `mosi` must return
to `1`.

## Documentation

| Document | Contents |
|---|---|
| `driver-firmware/docs/ARCHITECTURE.md` | Firmware design, timing and wire protocol |
| `driver-firmware/docs/APPLICATION.md` | Application instructions and host code design |
| `driver-firmware/docs/ISSUES.md` | Each defect found, its cause and its resolution |

Read `ISSUES.md` before you change the capture path. Several obvious
simplifications were tried before, and each one failed.

## Repository layout

```
driver-firmware/
  src/main.cpp                 firmware
  src/pixel_trig.pio           PIO trigger debounce
  tools/spectrometer_app.py    application launcher
  tools/spectrometer/          application modules
  tools/build_pio.py           regenerates the PIO headers before a build
  tools/live_view.py           minimal development viewer
  docs/                        documentation
```
