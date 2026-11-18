# UART control

The instrument firmware uses hardware UART1 through Arduino Serial2:
GPIO 4 is TX and GPIO 5 is RX, at 115200 baud, 8 data bits, no parity,
1 stop bit, with no flow control. Firmware starts without waiting for USB.
USB CDC is no longer the instrument command port.

Connect a 3.3 V USB-to-UART adapter TX to GPIO 5, RX to GPIO 4, and GND to
board GND. Use GPIO numbers to locate these signals on your board; chip
package pin numbers and Pico header pin numbers differ. Do not use RS-232
voltage levels or a 5 V TX signal.

## Sending commands

Send one ASCII command per line, terminated by LF or CRLF. Text replies end
in CRLF. Commands are case-sensitive, use lowercase command names, and are
not echoed. Use single spaces as shown; do not add leading/trailing spaces.
In the tables below, `<value>` is a placeholder and `[argument]` is optional;
do not send the brackets. Send valid numeric values: the parser does not
strictly reject malformed numbers and may interpret them as zero.

UART starts in quiet mode. Scanning continues internally, but no live
spectrum frames or per-frame diagnostics are transmitted. The GUI can use
the adapter's COM port at 115200 baud for commands, captures, calibration,
and SD operations; its live spectrum view is unavailable on this interface.

Commands are processed between scans. Allow time for replies and send them
sequentially; the receive buffer is 1024 bytes. Keep command lines short.
Although the parser allocates 384 bytes, its current length counter is only
8 bits: keep each command below 256 characters, excluding its line ending.
Do not rely on safe handling of overlong lines.

## Identification and scan control

| Command | What it does and reply |
| --- | --- |
| `id` | Returns an `I ` line with firmware version, protocol, board, pixel count, ADC limits, scan timing, capture limits, calibration/SD support, mode, and boot self-test result. |
| `ver` | Alias for `id`; returns the same full identification line. |
| `rate <hz>` | Sets the internal scan rate. Values are clamped to 0?60 Hz; 0 scans continuously. Nonzero rates also respect the minimum 20600 us frame period, so the requested rate can exceed the achievable rate. Returns `A rate_hz=...`. Default at boot: 15 Hz. Rejected during capture. |
| `period <us>` | Sets the frame period/integration interval in microseconds. Nonzero values are clamped to 20600?10000000 us; 0 scans continuously. Returns `A frame_us=... rate_hz=...`; the Hz field is an integer approximation. Rejected during capture. |
| `live 0` | Keeps UART quiet and returns `A live=0`. Scanning continues. Rejected during capture. |
| `live 1` | Returns `E live unsupported_uart`. Any nonzero live argument is unsupported; it never enables live frames. During capture, returns `E live busy_capturing`. |

`rate` and `period` change the same scan scheduling setting. A capture uses
its own exposure period and restores the previous setting when it ends.
Mode values in replies are 1 for quiet and 2 for capture (0 is the legacy
live mode, which UART cannot enable).

## Stacked captures

| Command | What it does and reply |
| --- | --- |
| `cap start secs=<s> [exp=<us>] [sigma=<f>] [block=<n>] [iters=<n>] [dark=<0|1>] [tag=<TT>]` | Starts a capture for approximately `<s>` seconds, converted to a frame count using the exposure. Returns `A cap started ...` with the actual settings, capture ID, warmup count, and tag. |
| `cap start frames=<n> [exp=<us>] [sigma=<f>] [block=<n>] [iters=<n>] [dark=<0|1>] [tag=<TT>]` | Starts a capture for a specified number of attempted frames. If both `frames` and `secs` are supplied, `frames` takes precedence. |
| `cap status` | Returns `C state=idle`, `run`, or `done`, plus ID, requested/attempted/stacked/excluded counts, exposure, elapsed time, result availability, clipping settings, and scan fault counters. |
| `cap stop` | Ends a running capture early and retains collected data. Returns `C state=stopped ...`; errors with `E cap not_running` when idle. A usable partial result can still be saved automatically. |
| `cap get` | **Binary download.** Sends `A cap sending id=... bytes=...` followed by the current RAM result as a binary stack record. Requires a completed usable result. Use a software client, not a plain text terminal. |

Capture arguments:

| Argument | Meaning, default, and limits |
| --- | --- |
| `secs` | Requested duration in seconds; fractional values accepted. Clamped to 0?600, converted to the nearest frame count, then clamped to the frame-count limits. One of `secs` or `frames` is required. |
| `frames` | Number of attempted frames, clamped to 1?16384. Frames rejected for acquisition faults can make the stacked count smaller. |
| `exp` | Frame period/integration interval in microseconds, clamped to 20600?10000000. Defaults to the current nonzero period, or 66666 us when continuous scanning is selected. |
| `sigma` | Positive sigma-clipping threshold, 1.00?20.00, default 3.00; stored to hundredths. |
| `block` | Frames per clipping block, 4?32, default 16. |
| `iters` | Maximum clipping iterations per block, 1?10, default 5. |
| `dark` | 0 for a normal spectrum (default); nonzero marks the result as a dark capture. This flag labels the result; it does not close a shutter or subtract a dark spectrum. |
| `tag` | Two-character SD filename prefix, uppercased. Defaults to `SP` for normal spectra or `DK` for dark captures. Use two letters or digits. |

Two warmup scans precede the counted frames. Starting a capture replaces the
previous result in RAM. A second start while capturing is rejected, as is a
start while a RAM-result transfer is active. Completion emits `C state=done
...`; `result=1` means there is a usable result. With SD auto-save enabled,
a save is attempted afterward and reported separately. No binary result is
sent automatically.

Example: capture 100 frames at a 100 ms interval and inspect the result:

```text
cap start frames=100 exp=100000 sigma=3 block=16 iters=5 tag=SP
cap status
```

Wait for `C state=done ...` before downloading the result or starting another
capture. `cap status` can be sent again while waiting.

## SD card and files

Files are stored under `/SPECTRA`. File arguments are basenames, not paths:
use at most 12 characters, consisting of uppercase A?Z, digits, `.`, `_`, or
`-`. Use conventional 8.3 names such as `SP000001.STK`. Directory listings
can include other files, but operations below require names accepted by this
validator. List/get/save/rename/delete attempt to mount an unmounted card.

| Command | What it does and reply |
| --- | --- |
| `sd` | Alias for `sd status`. |
| `sd status [free=1]` | Returns a `D state=...` line with insertion/mount state, card details, auto-save setting, codec, file/write counters, last saved file, and error details. `free=1` also calculates free space, which can take time. |
| `sd mount` | Attempts to mount the card and reports status. On failure returns `E sd mount_failed reason=...`. |
| `sd unmount` | Releases the card and returns `A sd unmounted`. Rejected while an SD download is active. |
| `sd auto <0|1>` | Disables/enables automatic saving of each usable completed or stopped capture. Default: enabled. Returns `A sd auto=...`. |
| `sd codec` | Reports the current write codec as `A sd codec=... n=...`. |
| `sd codec none` | Writes new files without compression. Existing files are unaffected. |
| `sd codec rle` | Selects lossless compression for new files (default). A save can fall back to uncompressed storage if compression does not help. Existing files keep their own codec. |
| `sd time <epoch>` | Sets the software clock used for FAT file dates. Supply local wall-clock time encoded as seconds since 1970-01-01; for local timestamps, add the local UTC offset to Unix time before sending. Minimum accepted value: 1000000000. Returns `A sd time=YYYY-MM-DDTHH:MM:SS`. Resets on reboot; this command does not set an RTC. |
| `sd list [start=<n>] [count=<n>]` | Lists files in directory order. Zero-based `start` defaults to 0; `count` defaults to 24 and is capped at 24. Emits `D file ...` entries with size, capture metadata, validity, and date, then `D list_end start=... shown=... total=...`. Use subsequent pages for more files. |
| `sd save [tag=<TT>]` | Saves the usable capture result currently in RAM as another numbered file. Default tag: `SP`, including for a dark result unless explicitly supplied. Returns `D saved name=... bytes=... plain=... codec=... id=...`, or an error. |
| `sd get name=<file>` | **Binary download.** Returns `A sd sending name=... bytes=...` followed by the decoded binary stack record. Rejects overlapping downloads. |
| `sd rename name=<file> to=<file>` | Renames one file without changing its capture record. The destination is uppercased. Returns `D renamed ...`; refuses to overwrite an existing destination. |
| `sd del name=<file>` | Permanently deletes the named file and returns `D deleted name=...`. There is no confirmation command. |

Automatic filenames have the form `TTNNNNNN.STK`, for example
`SP000001.STK`. Card insertion/removal also emits `D card ...` notifications.
Auto-save, codec, and the software clock are runtime settings; calibration
has a separate persistent flash store.

## Wavelength calibration

| Command | What it does and reply |
| --- | --- |
| `cal` | Returns the calibration as `K cal n=... bytes=... store=... points=...`. |
| `cal get` | Same as `cal`. |
| `cal status` | Same as `cal`. |
| `cal set <pixel:nm,pixel:nm,...>` | Replaces the calibration reference pairs in RAM and saves them to flash. Requires at least two pairs. Use comma-separated numbers without spaces, for example `cal set 100:400,900:700`. Returns the `K cal ...` line on success. |
| `cal clear` | Clears calibration in RAM and removes its flash file. Returns `K cal ... points=none` on success. |

The device stores reference pairs for the GUI's wavelength mapping; it does
not resample the acquired pixels. Validation permits numeric characters,
`.`, `:`, `,`, signs, and exponent notation, but does not fully validate pair
structure or physical meaning. Keep the complete command below the line
limit noted above. `store=1` means the flash filesystem is available.
`E cal store_failed` means persistence failed; a set/clear has already changed
RAM, so it must not be treated as a successful persistent save.

## Hardware diagnostics

These commands operate on the ADC and its GPIO signals. They can change
acquisition timing. `selftest`, `sditest`, `adcreset`, `cfrloop`, and `pindiag`
are rejected during capture; other diagnostic setters are not blocked, so
run diagnostics between captures to keep acquisition settings consistent.

| Command | What it does and reply |
| --- | --- |
| `cfr` | Reads the ADC configuration register eight times. Returns `H cfr ... match=<n>/8 (expect EFD)`, masking unimplemented bits for the match count. |
| `cfrw <hex>` | Writes the low 12 bits of a hexadecimal value to the ADC configuration register and immediately reads it back. Returns `H cfrw wrote=... readback=... MATCH/MISMATCH`. Unimplemented bits can cause a raw mismatch; the next pixel read restores the ADC defaults. |
| `cfrloop <seconds>` | Repeats configuration-register reads for 1?10 seconds (clamped), then reports transfer/match counts as `H cfrloop ...`. Pauses normal scans and command processing during the loop; useful for probing SDI and CS with a scope. |
| `sditest` | Tests ADC command decoding and writable register bits through the SDI path. Emits `H sditest ...` detail lines and PASS/FAIL. |
| `adcreset` | Performs a verified ADC software reset. Emits `H adcreset ...` lines for arming, the post-reset signature, and restored defaults. |
| `selftest` | Runs the SDI test and verified ADC reset, as at boot. Emits detailed `H ` lines and an overall `H selftest=PASS/FAIL ...`. |
| `pindiag` | Tests ADC GPIO drive and internal-pull readback and reports `H pindiag ...` lines. MISO is not driven. Restores idle pin configuration and ends with `H pindiag=done pins_restored`. |
| `sclk <nops>` | Sets extra delay instructions per SCLK half-period, clamped to 0?1000; 0 is the normal fast clock. Returns `A sclk_nops=...`. Large values can cause missed pixels. |
| `readdelay <us>` | Sets the settling delay before ADC reads, clamped to 0?10 microseconds. Returns `A readdelay_us=...`. |
| `verbose <level>` | Sets the legacy frame-text verbosity, capped at 2 (0 off, 1 status, 2 extra diagnostics). Returns `A verbose=...`. It does not enable frame or per-frame text output on quiet UART. |

## Replies and errors

| Prefix | Meaning |
| --- | --- |
| `BOOT:` | Startup diagnostics. |
| `I ` | Identification and capabilities. |
| `A ` | Acknowledgment or applied setting. |
| `E ` | Error; the rest of the line identifies the command and reason. |
| `C ` | Capture status/completion. |
| `D ` | SD status, file entries, saves, or card notifications. |
| `K ` | Calibration reference pairs and storage state. |
| `H ` | Hardware diagnostic results. |

Unknown commands return `E unknown_command=...`; unknown subcommands return
`E cal unknown_subcommand`, `E cap unknown_subcommand`, or
`E sd unknown_subcommand` when the SD handler reaches that check. Common
capture errors include `already_running`, `need_secs_or_frames`,
`still_running`, `no_result`, and `send_in_progress`. SD errors include
`not_mounted`, `bad_name`, `not_found`, `name_taken`, and operation-specific
failures. Read the actual acknowledgment to see any clamped settings.

The firmware has no UART `help` command. The GUI's console help is supplied
by the application, not the device.

## Explicit binary downloads

Only `cap get` and `sd get` request binary output. A stack record is 12356
bytes: a 64-byte header, 1024 pixel entries of 12 bytes each, and a 4-byte
checksum. The magic bytes are `A5 5A C7 2E`. Pixel entries contain a float32
mean, float32 standard deviation, uint16 accepted count, and uint16 rejected
count; fields are little-endian. Use the existing Python device client to
parse records rather than displaying them in a terminal.

Wait for the complete announced byte count before sending another command.
Text is suppressed while a binary record is being queued, to avoid corrupting
the record. Each transfer has a 10-second firmware timeout. UART remains
115200 baud, 8N1 for both text and binary output.
