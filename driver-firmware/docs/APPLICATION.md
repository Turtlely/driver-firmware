# Helios Application

Host software for the S3903-1024Q detector. Covers operation and the
design of the application code. Firmware internals are in
`ARCHITECTURE.md`; defect history is in `ISSUES.md`.

## 1. Running

```
python tools/spectrometer_app.py
```

Requires `pyserial` and `numpy`. Tkinter ships with the standard
python.org installer on Windows.

Select a port and press Connect. Ports with the Raspberry Pi vendor ID
(`0x2E8A`) are listed first, so the correct device is normally already
selected.

If connecting reports that the port is already open, another program holds
it. Windows grants a COM port to one process at a time. Close any serial
monitor or second copy of the application and retry.

**Desktop shortcut.** `powershell -ExecutionPolicy Bypass -File
tools\make_desktop_shortcut.ps1` creates `Helios.lnk` on the
desktop, pointing at the `.venv` Python and `tools/spectrometer/icon.ico`.
Uses `pythonw.exe` so no console window opens alongside the GUI. Safe to
re-run; it overwrites its own shortcut rather than duplicating it.

**Icon.** `python tools/make_logo.py` writes both
`tools/spectrometer/logo.png`, which the window and the taskbar use, and
`tools/spectrometer/icon.ico`, which the shortcut uses. It draws every
size at its own resolution rather than scaling one large image down,
because the mark is five hard colour boundaries and a resampled boundary
is a smear. Run it only if the mark changes. It needs Pillow, which is a
build-time tool for this one step and not a runtime dependency.

## 2. Layout

| Region | Contents |
|---|---|
| Menu bar | File, Device, Calibration, View, Help (diagnostic legend, About) |
| Left column | Preview, Capture, Reference correction, Statistics |
| Top right | Intensity strip and spectrum plot, horizontally aligned |
| Bottom right | Calibration, Distribution, Peaks, Files, Console tabs |
| Status bar | Port, frame rate, frame count, fault indicators |

The menu bar holds one-shot actions and settings that change rarely:
opening and saving a file, choosing a port, connecting, moving the
calibration between this computer and the instrument, and the theme.

The strip and the plot share a pixel axis and are aligned to the same
origin, so a feature in one can be read directly against the other.

Controls needed during a measurement are always visible. The tabs hold
work normally done once per session rather than per measurement.

## 3. Zooming the plot

Click and drag on the plot to select a rectangular region; releasing zooms
to it, on both axes. The strip above the plot always shows the full frame
and outlines the current zoom region on it, so context is never lost while
zoomed in.

Return to the full view with the Home button next to the plot,
right-click anywhere on the plot, or reconnect (a different device may
have a different pixel count, so a zoom in pixel units does not carry
over).

A drag shorter than a few pixels is treated as an ordinary click rather
than a zoom, so clicking to set the calibration pixel field still works
without needing a steady hand.

The vertical axis is not autoscaled at the home view for the reasons in
section 12.1; zooming is a separate, explicit action and does not change
that default.

## 4. Device menu

Choose the port from **Device > Port** and press **Device > Connect**. The
port list is read when the menu is opened, so a device plugged in after
startup needs no rescan.

Connect queries the device for identification rather than waiting for its
boot banner, because the device may have been running long before the
application started. There is no Identify command: the device identifies
itself when the port opens and whenever it sends an `I` line. What it
reported is shown at the top of the Console tab: firmware version, board,
pixel count, resolution, reference voltage, system clock, build date, the
PIO program checksum, and the result of the boot self test.

### 4.1 The boot self test

The device runs an SPI self check and then an ADC reset on every boot,
before it takes a single frame. It costs tens of microseconds.

The self check is the only thing that tests the SDI direction. A pixel read
does not drive SDI at all, so every frame proves SDO works and proves
nothing about SDI. **An SDI fault is silent.** The line stuck high sends
command 1111b, default mode, which the ADS7279 answers with plausible
conversion data, so a broken instrument goes on producing normal-looking
spectra. Running the check only when someone remembers to type `sditest`
means running it after the data is suspect rather than before it is taken.

The reset puts the part into a known state. Firmware wrote no ADC
configuration at boot before firmware 1.4, so the first scan after a power
cycle used whatever the ADC came up in. The reset also consumes the
all-ones cycle the datasheet says SDO produces immediately afterwards;
without that, the first pixel read returns `FFFC`, which is full scale and
looks like a real bright spike.

Nothing in the sequence waits on an external signal, so a missing or dead
ADC reports FAIL rather than hanging the boot.

The outcome is carried in the `I` line as `selftest=<0|1>` and shown on the
device label, because the boot lines scroll past long before anyone
connects. A failure is also logged once on connect. Run it again at any
time with the `selftest` console command; `sditest` and `adcreset` still
run each half on its own.

The lines it emits are the existing `A sditest` and `A adcreset` lines,
followed by one summary:

```
A selftest=PASS spi=PASS adcreset=PASS
```

If SDI is dead, the reset cannot be armed and reports
`inconclusive_not_armed`, which says nothing about why. That is why the
self check runs first: it names the fault the reset can only fail to
diagnose.

The PIO checksum is worth noting after firmware changes. It is computed
over the instruction words actually linked into the running binary. If a
`.pio` source is edited and the checksum does not change, the running
firmware does not contain the edit. See `ISSUES.md` entry 25.

## 5. Preview

**FPS and integration.** The two fields represent one frame period and
update each other. FPS is the default input.

40 Hz is the clean ceiling, measured with zero dropped frames, zero
missing pixels and zero checksum errors. Above 44 Hz the frame period
closes on the 20.5 ms scan and pixels begin to be missed, roughly 0.8 per
frame at 48 Hz. Those losses are marked rather than silent, so higher
rates remain usable if the trade is acceptable, but 40 Hz is the point
below which the data is complete.

Transmission used to bind before the sensor did, limiting the achievable
rate to about 30 fps. Protocol 2 halved the frame size and moved the limit
back to the scan, which is fixed by the CPLD pixel clock at 20 microseconds
per pixel and cannot be raised in firmware.

**Stack.** Acquires a fixed number of preview frames and combines them with
sigma-clipped averaging, which rejects per-pixel outliers before
averaging. Use this for a final measurement rather than reading the live
preview.

**Pause and Play.** The button says what pressing it will do. It reads
**Pause** only while live data is on screen. It reads **Play** when the
device is not transmitting, and also when a stacked result or a file opened
from disk is held over the plot, because in that state nothing on screen is
updating even though the device is still sending.

Pause stops the device transmitting frames. The display holds what it last
showed. The device keeps scanning either way, so the detector is never left
integrating with nothing to flush it. Play puts a held spectrum away and,
if the device was stopped, starts the frames again.

## 6. Reference correction

Applied in this order, which is not interchangeable:

```
raw codes -> volts -> dark subtraction -> flat field
```

Dark subtraction removes an additive offset and must happen on raw volts.
Flat fielding corrects multiplicative gain and is only meaningful once the
offset is gone.

**Dark.** Acquire with the detector shielded from light. Removes fixed
pattern offset and dark current. Combined by sigma-clipped median, which
is the conservative choice for a reference frame.

**Flat.** Acquire under uniform illumination at a level well below
saturation. Corrects per-pixel sensitivity variation. The flat is
dark-corrected before normalisation, or the dark offset would be baked
into the gain map as though it were response. Pixels below 5 percent of
the median are excluded, since dividing by them amplifies noise without
correcting anything.

A flat exposure with no usable signal is rejected rather than applied.

References can be saved and reloaded as `.npy`, which preserves NaN
exactly.

## 7. Calibration

Maps pixel index to wavelength using a polynomial fitted to reference
points from a source with known emission lines.

Click the plot to fill the pixel field, enter the wavelength, press Add.
Two points minimum. The fit and per-point residuals are shown.

Polynomial degree is chosen as `min(3, points - 2)`, not `points - 1`. A
polynomial of degree n-1 through n points fits exactly and reports zero
residual regardless of data quality, which hides bad reference points.
Keeping the fit overdetermined makes the RMS residual a real measure of
calibration quality.

Two points are the exception. A line through two points is exact and there
is no lower degree available, so a zero residual there means "not yet
verifiable", not "perfect". Add a third point before trusting the fit.

Calibration persists in `~/.s3903_spectrometer/settings.json`.

### 7.1 Calibration on the device

A calibration describes the optics, not the computer that measured them,
so the instrument keeps its own copy. It lives in the RP2350's flash, in
`/cal.txt`, and not on the memory card: a card can be swapped or removed,
and the calibration has to stay with the instrument when it is.

| Action | Where |
|---|---|
| Write the current points into the device | Calibration > Save to device, or the button on the Calibration tab |
| Read the device's points back | Calibration > Load from device |
| Erase the device's copy | Calibration > Clear on device |
| Write the points to a file on this computer | Calibration > Export to file |
| Read points from such a file | Calibration > Import from file |

Every connect reads the device's calibration and adopts it, so plugging in
a calibrated instrument calibrates the window. A device that holds nothing
leaves the local points alone.

The label beside the buttons on the Calibration tab says whether the
instrument agrees with what is on screen. "device holds these N points"
means the two match. "device holds N different points" means points have
been edited here and not sent.

What is stored is the reference POINTS, not the fitted polynomial. The
polynomial follows from the points and from the degree rule above, and
that rule lives in one place, `calibration.py`. A device holding
coefficients as well could hold two answers that disagree, with nothing to
say which was right.

Up to 16 points fit. The whole list travels as one command line, and the
firmware discards an over-long line rather than truncating it, so the
window refuses to send a list that would not fit rather than letting it
fail silently.

If the flash filesystem will not mount, the device reports `cal_store=0`.
The commands still work and the calibration still applies, but nothing
survives a power cycle, and the window says so.

## 8. Pixel distribution

Click a pixel on the plot (the same click that sets the calibration pixel
field) to open its value history on the Distribution tab. Three views,
selected by the radio buttons above the plot.

### 8.1 Histogram

The default view: that pixel's readings over time, with a Gaussian curve
at the sample mean and standard deviation drawn over it for comparison by
eye.

This is corrected per-frame data. The tab keeps its own history of up to
500 frames per pixel.

A Gaussian is the expected shape after the fixed pattern correction in
section 12.6: measured excess
kurtosis +0.01. Noise of thermal and amplifier origin is a sum of many
small independent contributions, which by the central limit theorem tends
to Gaussian regardless of the shape of any individual source.

Without the correction the histogram becomes visibly flat topped, excess
kurtosis about -1.3. That is not a second noise source but the 17.68 kHz
interferer described in section 12.6: a sinusoid sampled at random phase
is arcsine distributed, which is flat topped with a dip at the mean. This
tab is how that fault was found, and it is a good check after any change
to the optics or wiring.

A histogram that is skewed, or has two clearly separated peaks rather
than a broad flat top, is a different matter and points at a stuck bit,
switching between two states, or saturation.

At least 8 samples are required before anything is drawn.

### 8.2 Spectrum: per-pixel (time)

The amplitude spectrum of the clicked pixel's per-frame history, by FFT:
what frequencies that one pixel's value is actually varying at, not just
how much. One sample per frame, so the frequency axis runs from the
frame-history's own frequency resolution up to the Nyquist limit, half
the frame rate -- at 20 Hz that is 10 Hz. Both axes are logarithmic: real
content on a spectrum typically spans decades on both scale (a peak
against a noise floor two or three orders of magnitude down, over a
frequency range that itself spans decades), which a linear plot
compresses into a single spike against a flat line.

The frequency scale is captured once when the history starts
accumulating, not re-read every redraw. The live frame rate fluctuates by
a percent or so from one second to the next; re-reading it on every
redraw was rescaling the whole axis continuously, which read as the plot
"shifting around." It is recaptured whenever the history clears (below),
so it cannot go stale.

This can only resolve periodicities up to a few Hz to a few tens of Hz:
vibration, thermal cycling, a flickering source, a beat against the frame
rate itself. It cannot show the 17.68 kHz interferer directly; that
frequency is above this axis's Nyquist limit before any data is
collected. Use section 8.3 for that. Because it tracks one specific
pixel's own history, this view needs a pixel clicked; the other two views
on this tab do not (8.1's histogram does, 8.3 does not, see below).

Since only two or three cycles of a sub-Hz component typically fit in the
500-frame history window, a peak found near the low-frequency end of
this view is often not distinguishable from a slow one-way drift
(ambient light warming up, thermal settling) rather than a genuine
periodic oscillation. A handful of cycles is not enough to tell the two
apart by eye; treat a peak below a few hundred mHz as "something slow is
happening" rather than "this pixel oscillates at exactly X Hz" unless it
holds steady across more than one clearing of the history.

### 8.3 Spectrum: per-frame (pixels, kHz)

The amplitude spectrum of the video signal WITHIN one frame, across the
whole array, averaged over the last 60 frames. This is the axis that
reaches into the kHz range: pixels are read out at the 50 kHz pixel
clock, not the frame rate, so this resolves frequencies up to that
rate's 25 kHz Nyquist limit. It is the same technique that found the
17.68 kHz interferer in `ISSUES.md` entry 31, applied live to whatever is
currently on the array.

**Does not need a pixel clicked**, unlike the other two views on this
tab. An earlier version analysed a 256-pixel window centred on whichever
pixel was clicked, on the assumption that a spatial artifact might be
localised to one region. Entry 31 had already ruled that out: the
interferer's amplitude is uniform across the array (54 codes whether
measured near pixel 50 or pixel 950), so there is no location for a
click to usefully narrow down, and it was only adding friction. Using
the whole array instead also roughly quadruples the frequency resolution
a 256-pixel window gave, around 49 Hz per bin instead of 195 Hz.

Averaged over periodograms rather than shown for one frame, because the
interferer's phase is random frame to frame (the same randomness that
makes the histogram arcsine-shaped when uncorrected); averaging magnitude
spectra across frames is standard practice for exactly this (Welch's
method) and turns a noisy single-frame estimate into a stable one.

**Sourced from uncorrected data, unlike the other two views.** The
fixed pattern correction (section 12.6) removes precisely the frequency
band this view exists to show. Confirmed live: computed
from the corrected buffer, this view could not see the interferer at
all, and its peak marker read out the array's own low-frequency
illumination shape instead, at whatever the lowest resolvable frequency
happened to be. The label reads `[raw, uncorrected]` as a reminder that
this is not the same signal the plot or an exported spectrum shows.

The peak marker only searches frequencies at or above 15 kHz. Below that,
real content -- the illumination profile and any real spectral features
-- routinely has more raw amplitude than the interferer has anywhere,
simply because it is real and large; marking the true global maximum
would point at "the array is illuminated," which is true and not the
point. The curve itself is drawn in full from its lowest resolvable
frequency, so nothing is hidden, only the automatic marker is
restricted. 15 kHz is not an arbitrary round number: it is
`processing.notch_pattern()`'s own established boundary, where 99.96
percent of a real frame's spectral energy already sits below it.

### 8.4 History and clearing

"Clear history" applies to all three views; they share the same
underlying per-frame buffers (a corrected one and an uncorrected one,
section 8.3). The history clears on reconnect and on any change to
exposure, since exposure changes what is being measured. Clear it manually after changing
anything else that might shift the distribution, such as reference
correction.

## 9. Peaks

Peaks are located by prominence: for each local maximum, the signal is
traced outward until it rises above the peak again, and prominence is the
height above the higher intervening minimum. Width is reported at half
prominence.

Prominence is used rather than absolute height because it measures a
peak's significance against its local surroundings, which is what matters
on a sloping baseline.

The minimum prominence threshold is set in volts on the tab. Peaks are
annotated on the plot and listed with pixel position, wavelength when
calibrated, height, prominence and width.

This is implemented directly rather than with scipy, keeping the
dependency list to numpy and pyserial.

## 10. Console

A scrollback of everything the device says, and a command line that
sends to it. The log and the input are one panel: a command and the
reply it caused belong in one scrollback in the order they happened,
and the device answers on the same serial stream the frame stats
arrive on.

The log shows the per-frame `S` lines from the firmware with a decoded
summary underneath. Fields are documented in `ARCHITECTURE.md` section
7.2.

Lines are coloured by kind, so a reply is visible in traffic that
scrolls at the frame rate: a sent command in blue, an `A` acknowledgement
in green, an `E` error in red, an `I` identification in purple, frame
stats in grey.

The values that matter during normal operation:

| Field | Healthy | Meaning if not |
|---|---|---|
| `miss` | 0 | Pixels not captured this frame |
| `period` | 20 us | Measured pixel period; instability indicates a timing problem |
| `fifo` | 1 to 2 of 8 | Approaching 8 means the host CPU is falling behind |
| `wdog` | 0 | Scan watchdog fired |
| `nostart` | 0 | CPLD ignored START and retries were exhausted |
| `txdrop` | stable | Frame text skipped because the host was not draining |

### 10.1 Command line

Type a command and press Enter. `help` lists what the firmware accepts:

| Command | Argument | Effect |
|---|---|---|
| `id`, `ver` | | Report firmware identification and current settings |
| `rate` | hz | Set frame rate in whole hertz; 0 runs free |
| `period` | us | Set the frame period; finer than `rate` at the slow end |
| `verbose` | 0 to 2 | Set how much text accompanies each frame |
| `cfr` | | Read the ADC configuration register 8 times |
| `cfrw` | hex | Write a configuration register value and read it back |
| `sclk` | nops | Set the SCLK half period, 0 to 1000 |
| `readdelay` | us | Set a settling delay before each read, 0 to 10 |
| `adcreset` | | Reset the ADC and verify the reset took effect |
| `sditest` | | Test SDI end to end: command field and data field |
| `cfrloop` | secs | Repeat the read-CFR transfer for 1 to 10 s, for scope work |
| `pindiag` | | Test the four ADC pins for a stuck net; needs no scope |

Anything not in this list is still sent. The firmware answers
`E unknown_command`, which is a better answer than the application
refusing to try, since the firmware is the authority on what it accepts.

Three commands are answered by the application itself and never reach
the device: `help`, `clear` and `history`.

Up and Down recall previous commands. Tab completes a command name, and
lists the candidates when more than one matches. Ctrl+L clears the log.

`rate` and `period` change device state that the Preview panel also
owns. After either is typed here, the application re-identifies the
device so the panel shows what is actually set rather than what was
last requested through it.

Frame stats arrive at the frame rate, so at 20 Hz a reply scrolls out
of sight in under a second. The Frame stats checkbox mutes them in the
log without stopping acquisition; the summary line below keeps showing
the newest values either way.

Copy places the log on the clipboard.

## 11. Saved spectra

Tab-separated text with a commented header, readable in a text editor and
loadable directly with `numpy.loadtxt` or `pandas.read_csv`.

The header records device, detector configuration, frame count,
integration time, correction state, calibration and detected peaks. A
spectrum without those is not reproducible, and they are the details
forgotten first.

Every header line is a field, `# key: value`, apart from the title on the
first line and the tab-separated peak rows. The header carried explanatory
sentences until firmware 1.4. They are gone. A sentence in a data file
cannot be parsed, it goes stale against the code that writes it, and it is
already here.

Missing pixels are written as `NaN`, never zero (`# missing_value: NaN`).
Zero is a legal reading and would bias any statistic computed over the
file.

### 11.1 Opening a file

**File > Open spectrum** reads two forms, neither of which needs a device
attached:

- `.STK`, the record the firmware writes to the card. It is decoded by
  `spectrometer/stkfile.py`, the same module `tools/read_stk.py` uses, so a
  card read in a card reader opens exactly like a stored file does. The
  capture counters come with it.
- `.txt`, a file this program wrote. Only the columns are read.

The spectrum is held over the plot, and the Preview button changes to
**Play**. Press it to go back to live data.

### 11.2 The Files tab

The card browser. Everything shown is read from the card, so a card
carried over from another instrument appears exactly like a file written a
minute ago.

The line at the top right says how the card is formatted and how full it
is, in GB. Measuring the free space walks the whole file allocation table
on the device and takes seconds on a large card, so it is measured on
Refresh and not on the status poll that runs during a capture.

### 11.3 Card size

**The ceiling is 2 TiB, and a 1 TB card works.** SdFat addresses the volume
with 32-bit sector and cluster numbers over 512-byte sectors, so 2^32 × 512
bytes is the hard limit. Cards above that are the SDUC class, which SdFat
does not support over SPI in any case.

A card of that size must be exFAT. FAT32 is not a practical option above
about 32 GB, and exFAT is what a 1 TB card ships formatted as. SdFat is
built here with `SDFAT_FILE_TYPE 3`, which compiles in FAT16, FAT32 and
exFAT, and the firmware already uses `SdFs` and `FsFile` throughout, so no
change is needed to read one. The device reports exFAT as `fat=64`, which
the window shows as `exFAT` rather than inventing a "FAT64".

Two things to know before fitting one:

**Free space costs far more on FAT32 than on exFAT.** exFAT keeps an
allocation bitmap, so a 1 TB volume is about 1 MB to read. FAT32 has no
bitmap and the whole file allocation table has to be walked: at a 32 kB
cluster that is 134 MB, which over SPI is minutes, and the read happens
inside `loop()` and stalls the scan for its duration. This is another
reason a large card should be exFAT. Free space is only measured on
Refresh, never on the status poll.

**The file name is the real ceiling, not the card.** Names are
`TTNNNNNN.STK`, so the sequence number runs out at 999,999 files, which at
roughly 8 kB each is about 8 GB. Everything above that is reachable only by
renaming files out of the sequence. A larger card buys margin and wear
levelling, not more spectra.

The card and the mounted volume are separate numbers, and the device now
reports both: `size_mb` is the physical card from its capacity register,
`vol_mb` is the mounted partition. Files live on the volume, so every "how
full" figure is measured against `vol_mb`. When a card carries a partition
materially smaller than itself the window says so — `FAT16, 0.78 of 3.7 GB
used (21.1 %) (card 117.8 GB)` — because that is a formatting fault: most
of the card is unreachable, and nothing else in the window would report it.

**Search** filters the listing already in hand, by name, kind or date. It
sends nothing to the device, so it stays instant while a capture runs.

**Sort** by clicking a column heading. Clicking the same one again
reverses it. The default is newest first, by the sequence number in the
name rather than by the date: the number always increases, while the date
is 2000-01-01 for anything written before the host handed the device a
clock.

**Rename** (button, or F2) gives a file a different name. FAT holds 8.3
names, so the new name is 1 to 8 characters from `A-Z`, `0-9`, `-` and
`_`; the extension stays `.STK`, because a spectrum with any other
extension stops appearing in the listing. The device refuses a name that
is already taken rather than overwriting it.

Renaming loses the sequence number the name encodes. Nothing depends on
it. The capture id inside the record is the identity, and the device reads
the directory to decide where to continue numbering.

## 12. Interpreting the display

### 12.1 Fixed vertical axis

The plot axis is fixed at 0 V to the reference voltage and is not
autoscaled. An autoscaled axis makes noise on a weak signal look identical
to structure on a strong one, hides how much of the converter range is in
use, and prevents two measurements from being compared by eye. The small
negative margin exists because dark subtraction can legitimately push
corrected values slightly below zero.

### 12.2 Step size

One code is `vref / 2^bits`. At 2500 mV and 14 bits that is 152.59
microvolts. The Device panel and the Statistics panel both report it, and
frame-to-frame noise is reported in codes as well as millivolts.

That second figure is the useful one. Noise near one code means the
measurement is at the quantisation floor and further averaging gains
little. Noise of many codes means something real is limiting it, and the
cause is upstream of the converter.

### 12.3 Frame rate, signal and noise

Measured on this instrument by sweeping 2 to 30 Hz under steady
illumination (`tools/measure_rate_response.py`):

| Rate | Exposure | Signal | Noise | SNR |
|---|---|---|---|---|
| 2 Hz | 500 ms | 1593.2 mV | 6.083 mV | 262 |
| 5 Hz | 200 ms | 1592.6 mV | 6.008 mV | 265 |
| 8 Hz | 125 ms | 1061.5 mV | 6.073 mV | 175 |
| 12 Hz | 83 ms | 714.0 mV | 6.031 mV | 118 |
| 20 Hz | 50 ms | 443.6 mV | 6.008 mV | 74 |
| 30 Hz | 33 ms | 309.1 mV | 6.026 mV | 51 |

**Noise does not change with frame rate.** It is 6.03 mV, about 40 codes,
at every rate, fitting exposure to the power 0.00. The instrument is
readout-noise limited, not shot-noise limited.

Frame rate therefore does not affect the noise. It affects the signal,
because exposure is the START interval, and the noise only appears to
change because it is being read against a larger or smaller signal. SNR
improves proportionally with exposure rather than as its square root,
which is the signature of a fixed per-conversion noise floor.

**The 6.03 mV figure above is not the instrument's real noise floor.**
About three quarters of that variance is a single 17.68 kHz interferer on
the analog video signal, not random noise. With it notched out, which is
the default, the interior of the array measures **1.32 mV, about 8.7
codes**. The table above is retained as measured, since it is what the
uncorrected instrument does and the flat-with-exposure conclusion is
unaffected: the interferer is additive and independent of exposure too.

This also revises what the noise is. A fixed per-conversion floor is
still the right description, but most of it turned out to be one
deterministic component rather than a thermal limit. See section 12.6 and
`ISSUES.md` entry 31.

Two consequences follow:

- Averaging N frames reduces noise by the square root of N and is the
  main lever available. At 40 codes of readout noise against a
  quantisation floor of one code, there is a great deal of headroom.
- Signal saturates near 1590 mV, only 64 percent of the 2500 mV converter
  range. Below saturation it is linear in exposure at roughly 8.2 mV per
  millisecond with a 36 mV offset. The remaining third of the converter
  range is unreachable, which is an analog gain or reference matter
  rather than a firmware one.

Longer exposure also accumulates dark current, so there is an optimum
rather than monotonic improvement.


Exposure is the interval between START pulses, so exposure is 1/rate.
Changing the rate therefore changes how much charge each pixel collects,
which is why the data looks quieter at low rates.

Two noise sources behave differently:

| Source | Behaviour with exposure | Effect on SNR |
|---|---|---|
| Readout and quantisation | fixed per conversion, independent of exposure | improves proportionally with exposure |
| Photon shot noise | grows as the square root of collected charge | improves as the square root of exposure |

In both cases signal-to-noise improves at lower frame rate, but for
different reasons, and the distinction matters. If readout noise dominates,
averaging frames and lowering the rate are interchangeable. If shot noise
dominates, the detector is already performing to its physical limit and
only more light or more integration will help.

Against this, longer exposure also accumulates more dark current, which
adds its own shot noise and a growing offset. There is therefore an
optimum rather than a monotonic improvement, and it is specific to the
illumination in use.

`tools/measure_rate_response.py` measures which regime applies by sweeping
the rate and fitting signal and noise against exposure. An exponent near
0.5 for noise indicates shot-noise limited; near 0.0 indicates readout
limited.

### 12.4 How SNR is calculated

Signal-to-noise is the mean signal divided by the **temporal** standard
deviation: the frame-to-frame spread of a single pixel, averaged over
pixels. Reported as a ratio and in decibels, using 20 log10 because these
are amplitudes rather than powers.

```
SNR = (mean_signal - baseline) / temporal_sd
```

It appears only once the frame average holds two or more frames, because
noise is the spread of repeated measurements of the same thing and one
frame cannot provide that.

**The spatial spread across pixels is not noise.** The Statistics panel
reports it separately, labelled as spread across px. On a real spectrum it
is dominated by spectral structure and the illumination profile, both of
which are signal. Using it as a noise figure understates SNR badly, and
the error grows with the amount of structure present: on a synthetic
spectrum with two emission features and 6.0 mV of injected noise, the
temporal method recovers 5.94 mV and 42.1 dB while the spatial method
reports 118.6 mV and 16.1 dB, a 26 dB error.

An earlier version of this application computed the displayed SNR from
spatial spread. It was wrong and has been corrected.

**Baseline.** The signal term has the baseline subtracted first, because
signal-to-noise is a property of the measured quantity and not of the
pedestal it sits on. With dark subtraction enabled the pedestal is already
removed and the baseline is zero. Without it, a large fixed offset inflates
the figure: 900 mV over 6 mV of noise reads 43.5 dB if the 700 mV pedestal
is ignored and 30.5 dB once it is removed.

For reference, this instrument measures 6.03 mV of temporal noise
independent of frame rate, giving SNR from 51 at 30 Hz to 262 at 5 Hz as
the signal grows with exposure (section 12.3).

### 12.5 End pixels

Pixels 0 and 1023 do not carry valid measurements and are masked by
default. The checkbox in Reference correction disables the mask for
inspection.

**Pixel 1023** held 202 to 217 mV at every unsaturated frame rate while
the array interior varied from 307 to 1592 mV. A pixel that does not
respond to exposure is not measuring light. It reads the video line
baseline, most likely because the final trigger samples after the last
element has been shifted out. Unmasked it reads about 29 percent of the
interior and drags the minimum and the mean.

**Pixel 0** read 121 percent of the interior against 110 percent for pixel
1, an excess of about 11 percent over its immediate neighbour that does
not scale with exposure. That is a readout artifact at the start of the
shift register.

The broad elevation across roughly the first 190 pixels is a separate
matter. It decays smoothly and is consistent with the illumination
profile rather than with a readout defect, so it is real signal. Correct
it with a flat field, not by masking.

Masked pixels are set to NaN rather than removed, so pixel indices
continue to match the detector and every downstream statistic excludes
them by construction.

### 12.6 Pattern noise notch

The 17.68 kHz correction is always applied. It removes a measured non-physical component of the analog
video signal, in the same category as dark subtraction rather than as
smoothing.

The instrument carries a single additive sinusoid at 0.3539 cycles per
pixel, a period of 2.825 pixels, 17.68 kHz at the 20 microsecond pixel
period. It is what you see as a small regular oscillation when zoomed
into the trace, and it accounts for about three quarters of the apparent
noise variance. It is also why an uncorrected pixel's histogram on the
Distribution tab is flat topped rather than the Gaussian described in
section 8: a sinusoid sampled at random phase is arcsine distributed.

Measured effect, on live hardware through this application's own path:

| | Interior noise | Excess kurtosis |
|---|---|---|
| Raw | 6.043 mV (39.6 codes) | -1.25 |
| Notched | 1.321 mV (8.7 codes) | +0.01 |

No pixel anywhere on the array is made worse. The outer 15 pixels at each
end improve by about a factor of 2 rather than 4.6, because a frame is
not periodic and the interferer has an arbitrary phase discontinuity
there. That residue is random frame to frame, so it averages away with
stacking like ordinary noise: what survives a 399 frame stack is 0.57 mV.

An earlier version of this correction left a much larger oscillation at
the two ends that did **not** average away. If you see that, the software
is out of date. See `ISSUES.md` entry 32.

**It does not remove real spectral content.** Interference and signal do
not overlap in spatial frequency: 99.96 percent of a real frame's energy
lies below 0.30 cycles per pixel, because emission lines span several
pixels and the illumination envelope spans hundreds, while the interferer
is a spike at 0.354. Synthetic emission lines added to real frames retain
100 percent of their area, and 98.7 percent of peak height at 3 pixel
FWHM, 100 percent at 5 pixels and wider. A line narrower than about 3
pixels is close to the pixel Nyquist limit and is not properly sampled by
this detector regardless.

The state is recorded in exported spectra as `pattern_notched`, so a
saved file always says whether it was applied.

The interferer's source is upstream of the Pico and has not been
identified, so this corrects the symptom. `ISSUES.md` entry 31 records
the diagnosis, the hypotheses rejected along the way, and what a proper
hardware fix would require.

## 13. Code structure

```
tools/
  spectrometer_app.py          launcher
  spectrometer/
    device.py                  serial transport and wire protocol
    processing.py              volts, references, accumulation, statistics, peaks
    calibration.py             wavelength fit, settings persistence
    export.py                  spectrum and reference file formats
    stkfile.py                 stacked capture record format
    typography.py              interface and readout fonts
    app.py                     user interface
    fonts/                     Inter, bundled under the SIL Open Font License
```

**Separation.** The user interface never touches a serial port. `Device`
owns the port on its own thread and publishes to queues; the interface
drains them on a timer. A slow redraw therefore cannot stall acquisition,
and a disconnect cannot deadlock the window.

**Queue overflow.** The reader drops the oldest entry rather than blocking
when a queue is full. A reader thread that stalls on a consumer would
overflow the operating system's serial buffer and lose frames at the port,
which is worse than dropping a stale frame the display would never have
shown.

**Missing pixels as NaN.** Propagated as NaN rather than zero throughout
processing. Zero is a legal reading and would silently bias every
statistic that touched it. NaN forces each consumer to handle the gap, and
numpy's nan-aware functions exclude them by construction.

**Trace breaks at gaps.** The plot breaks the trace at missing pixels
rather than interpolating across them. A straight line over a gap is
invented data.

**Two fonts, on purpose.** The interface uses Inter, bundled in
`tools/spectrometer/fonts/` and loaded for this process only, so nothing
is installed on the machine. The console and every numeric readout stay
on Consolas. Those readouts are columns of digits that change while you
watch them, and a proportional font makes the columns move as the digit
widths change. Do not move the readouts to the interface font.
`typography.py` holds the rule and the fallbacks used where Inter cannot
be loaded.

## 14. Command protocol

Requests are plain text lines. Responses share the frame stream.

### 14.1 Line prefixes

Every line the device sends starts with one letter naming what it is. The
host routes and colours by that letter alone, so a line under the wrong one
is not a cosmetic problem.

| Prefix | Meaning |
|---|---|
| `I` | Identification. The `I` line is a contract; see below. |
| `S` | Per-frame statistics, one per frame at verbosity 1 |
| `T` | Per-frame trigger count, one per frame |
| `C` | Capture status |
| `D` | Card: `state`, `file`, `list_end`, `saved`, `deleted`, `renamed` |
| `K` | Calibration |
| `H` | Hardware test result: `sditest`, `adcreset`, `selftest`, `cfr`, `cfrw`, `cfrloop`, `pindiag` |
| `A` | Acknowledgement of a command that changed a setting |
| `E` | Error |

`T` and `H` are new in firmware 1.4, and both were carved out of prefixes
that did not describe them:

- The trigger count went out as `E frame …`. The host classified it as an
  error and drew it red and bold, under the Errors filter, at one line per
  frame. It is a routine diagnostic, so it now shares the Frames filter with
  the `S` line — which is off by default, and is why it no longer floods the
  console.
- The hardware tests went out as `A …`. Acknowledgements are drawn in the
  success colour, so a line ending `FAIL` was green. They now carry `H`, and
  the console colours each one by its own verdict rather than by its prefix.

The other prefixes were left alone. A rename that fixes nothing costs every
script that reads the stream.

| Command | Response |
|---|---|
| `id` | `I` line with device identification |
| `rate <hz>` | `A rate_hz=<n>`, 0 means free running |
| `selftest` | `A sditest`, `A adcreset`, then `A selftest=<PASS\|FAIL> spi=… adcreset=…` |
| `cal get` | `K cal n=<points> bytes=<n> store=<0\|1> points=<list>` |
| `cal set <px>:<nm>,...` | the same `K` line, or `E cal bad_points` |
| `cal clear` | the same `K` line |
| `sd rename name=<f> to=<f>` | `D renamed name=<f> to=<f>` |
| unrecognised | `E unknown_command=<text>` |

The full list is in the window: **Console > Commands**.

Calibration replies use `K`, not `A`, because the reply carries the whole
point list and needs a parser of its own rather than being logged as an
acknowledgement. The point list is `<pixel>:<nm>` pairs separated by
commas; the firmware validates the character set and the length and stores
the bytes, and never parses a number out of them. Nothing on the device
uses a wavelength.

The command line buffer is 384 bytes, sized so `cal set ` and a full
16-point list both fit. An over-long line is discarded, not truncated, so
a request that does not fit never arrives at all.

Frames are binary in protocol 2; status lines, replies and identification
remain ASCII on the same stream. All of it is emitted through one output
queue in firmware, so a reply cannot be interleaved into a frame. An
earlier version wrote replies directly to the port and corrupted both, and
the first correction for that suppressed command handling while the queue
was busy, which made the device uncommandable at high frame rates. See
`ISSUES.md` entries 27 to 30.

The `I` line is a contract with the application. Adding key=value fields
is safe; renaming or reordering existing ones is not. Values must not
contain spaces, since the line is space-separated (see `ISSUES.md` entry
26).

Commands are handled in `loop()`, between frames, so a command cannot
perturb pixel timing inside a scan.
