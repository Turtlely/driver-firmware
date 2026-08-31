# Helios

Firmware and host software for a line-scan spectrometer built around a
Hamamatsu S3903-1024Q linear photodiode array. The instrument is driven by a
Raspberry Pi Pico 2 (RP2350) with an ADS7279 14-bit ADC and a CPLD driver
board. Spectra can be viewed live over USB or recorded to the instrument's
own memory card and read back later.

The repository holds three things:

- **Firmware** for the Pico, in `driver-firmware/src/`.
- **Helios**, the desktop application that drives the instrument, in
  `driver-firmware/tools/spectrometer/`.
- **Command-line tools** for development, measurement and file recovery, in
  `driver-firmware/tools/`.

For the design of either half, see
[`docs/ARCHITECTURE.md`](driver-firmware/docs/ARCHITECTURE.md) for the
firmware and
[`docs/APPLICATION.md`](driver-firmware/docs/APPLICATION.md) for the host
software.

## Requirements

To run the application from source you need Python 3.12 or later with
Tkinter, plus the packages in `driver-firmware/requirements.txt`. The
installers from python.org include Tkinter on Windows and macOS. On Debian
and Ubuntu it is a separate package:

```
sudo apt install python3-tk
```

To build and flash the firmware you need [PlatformIO](https://platformio.org)
and a Raspberry Pi Pico 2.

## Installation

### From source

Clone the repository and install the dependencies. A virtual environment is
recommended but not required. The repository root holds a single
`driver-firmware/` directory containing the project, so paths below are
written from the root and repeat the name once.

```
git clone https://github.com/Turtlely/driver-firmware.git
cd driver-firmware

python -m venv driver-firmware/.venv
driver-firmware\.venv\Scripts\activate        # Windows
source driver-firmware/.venv/bin/activate     # macOS and Linux

pip install -r driver-firmware/requirements.txt
```

Start the application:

```
python driver-firmware/tools/spectrometer_app.py
```

On Windows you can add a desktop shortcut. Run this once; it is safe to
re-run and overwrites its own shortcut rather than making duplicates.

```
powershell -ExecutionPolicy Bypass -File driver-firmware/tools/make_desktop_shortcut.ps1
```

### As a standalone Windows application

The application can be packaged into a build that runs on a computer with no
Python installed. Install the build tools, then run the packager:

```
pip install -r driver-firmware/requirements-dev.txt
python driver-firmware/tools/make_exe.py
```

The result is `driver-firmware/build/exe/dist/Helios/`. The folder is
self-contained: copy it anywhere, or zip it to hand to someone else, and run
`Helios.exe` inside it.

Passing `--onefile` produces a single `Helios.exe` instead of a folder. It is
smaller to send (26 MB against 63 MB) but noticeably slower to start, because
it unpacks itself into a temporary directory on every launch — about 2.2
seconds, against 0.3 seconds for the folder. Prefer the folder unless a
single file is a requirement.

The executable is not code-signed, so the first launch on another machine
will raise a Windows SmartScreen warning.

## Using the application

Open the **Device** menu, pick a port under **Port**, then choose
**Connect**. Ports with the Raspberry Pi vendor ID (`0x2E8A`) are listed
first, so the instrument is normally already selected. It reports its own
configuration as soon as the port opens.

The application provides a live view, frame averaging, and stacked capture of
a fixed number of frames, with dark and flat-field correction, wavelength
calibration, peak detection and export to text. Captures can also be written
to the instrument's memory card and browsed from the **Files** tab.

Full instructions are in
[`docs/APPLICATION.md`](driver-firmware/docs/APPLICATION.md).

**One program at a time.** Windows grants a serial port to a single process.
Close the application before running any of the command-line tools, and close
any serial monitor before starting the application. If connecting reports
that the port is already open, something else still holds it.

## Building the firmware

```
cd driver-firmware
pio run -t upload
```

The build regenerates the PIO headers first. Do not run `pioasm` by hand.

If the upload fails because picotool cannot claim the device, flash the board
manually: hold BOOTSEL while connecting the board, then copy
`.pio/build/pico2/firmware.uf2` to the drive that appears.

## Command-line tools

Run these from inside the `driver-firmware/` directory. Every tool that
talks to the instrument needs the serial port to itself; `read_stk.py` is the
exception, since it reads a memory card directly and never opens a port.

| Command | Purpose |
|---|---|
| `python tools/live_view.py [port] [baud]` | Minimal viewer for development. Finds the port if given no argument. |
| `python tools/read_stk.py <path>` | Lists and decodes `.STK` capture files straight off a memory card. |
| `python tools/measure_pixel_noise.py [pixel ...]` | Records selected pixels over time and reports their distribution. |
| `python tools/measure_rate_response.py` | Measures the effect of frame rate on signal and noise. Takes about two minutes. |
| `python tools/make_logo.py` | Redraws the application icon. Needed only if the mark changes. |
| `python tools/make_exe.py` | Packages the application as a Windows executable. |

## Tests

```
python driver-firmware/test/run_tests.py
```

Ten checks cover the capture arithmetic, the stored record format, the
memory card workflow and the application's own control flow. Six of them
compile firmware code for the host, extracted from `src/main.cpp` on every
run so that a stale copy cannot pass silently. Those six are skipped when
`g++` is not on the PATH, and the other four still run. See
[`test/TESTS.md`](driver-firmware/test/TESTS.md).

## Serial commands

Send one command per line at 115200 baud. The instrument answers with a line
beginning `A `; an error begins `E `.

| Command | Function |
|---|---|
| `id` | Reports the firmware version and all acquisition parameters. |
| `rate <hz>` | Sets the frame rate. Use `0` for free running. |
| `period <us>` | Sets the frame period in microseconds. This also sets the integration time. |

The commands below exist to test the hardware and are not needed in normal
operation.

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

> **Warning:** `sclk`, `readdelay` and `mosi` change how the instrument
> captures data. Set each back to `0` when you are finished. `mosi` must be
> returned to `1`.

## Documentation

| Document | Contents |
|---|---|
| [`docs/ARCHITECTURE.md`](driver-firmware/docs/ARCHITECTURE.md) | Firmware design, timing and wire protocol |
| [`docs/APPLICATION.md`](driver-firmware/docs/APPLICATION.md) | Application instructions and host code design |
| [`docs/ISSUES.md`](driver-firmware/docs/ISSUES.md) | Every defect found, its cause and its resolution |
| [`test/TESTS.md`](driver-firmware/test/TESTS.md) | What the checks cover and how they are built |

Read `ISSUES.md` before changing the capture path. Several obvious
simplifications have been tried already, and each one failed.

## Repository layout

```
driver-firmware/
  src/main.cpp                 firmware
  src/pixel_trig.pio           PIO trigger debounce
  platformio.ini               firmware build configuration
  requirements.txt             runtime dependencies for the host tools
  requirements-dev.txt         build tools: icon and executable
  tools/
    spectrometer_app.py        application launcher
    spectrometer/              application modules
      app.py                   user interface
      device.py                serial transport and wire protocol
      processing.py            volts, references, statistics, peaks
      calibration.py           wavelength fit and settings
      export.py                spectrum and reference file formats
      stkfile.py               stacked capture record format
      typography.py            interface and readout fonts
      resources.py             locates bundled files, packaged or not
      fonts/                   Inter, under the SIL Open Font License
    build_pio.py               regenerates the PIO headers before a build
    live_view.py               minimal development viewer
    read_stk.py                decodes capture files from a memory card
    make_logo.py               draws the icon
    make_exe.py                packages the Windows executable
  test/                        host-side checks
  docs/                        documentation
```

## License

MIT. See [`LICENSE`](LICENSE).

The bundled Inter typeface is licensed separately under the SIL Open Font
License 1.1; see `driver-firmware/tools/spectrometer/fonts/OFL.txt`.
