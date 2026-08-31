# Codebase Simplification Review

## Scope

This review covers firmware, host software, utilities, tests, and documentation.
The referenced video could not be retrieved through the available web interface.

## Findings

### 1. Split the application class

`tools/spectrometer/app.py` contains about 3,500 lines and 139 methods in one
class. It combines widget construction, acquisition state, storage operations,
console behavior, calibration, plotting, and export coordination.

Recommended modules:

- `ui/main_window.py`: layout and workflow panels.
- `ui/plots.py`: spectrum, histogram, and FFT rendering.
- `capture.py`: preview stack and device capture state machines.
- `storage.py`: SD listing, transfer, and deletion workflow.
- `console.py`: command entry, filtering, and diagnostic display.

Keep one application controller that owns these components. Do not introduce a
general event framework. Tk callbacks and explicit method calls are sufficient.

### 2. Remove the legacy viewer

`tools/live_view.py` is an 811-line second application with its own transport,
protocol parsing, diagnostics, and rendering. Those functions now exist in the
Helios package. Retaining both implementations creates two protocol clients that
can disagree.

Archive the file with the measurement records that depend on it or replace it
with a small launcher that uses `spectrometer.device` and shared renderers.

### 3. Consolidate capture paths

The host preview stack and the device capture implement similar acquisition
outcomes with separate state and error paths. The host path also acquires dark
and flat references while the device path supports the same output types.

After device capture is proven on all supported firmware versions route spectrum
dark and flat acquisition through one capture controller. Retain host stacking
only as an explicit compatibility path. This removes `capture_frames`,
`capture_target`, and duplicate completion logic from the main application.

### 4. Complete firmware state ownership

`src/main.cpp` contains state structures followed by temporary reference aliases
for their members. The comment at the alias block already identifies the intended
consolidation. Convert remaining users to `frameState`, `adcDiagnostics`,
`diagnosticSettings`, and `txQueue` then delete the aliases.

This is a low-risk first firmware cleanup because it changes names and ownership
without changing timing or protocol behavior.

### 5. Split firmware by stable subsystem

`src/main.cpp` is approximately 4,000 lines. Split only at established hardware
boundaries:

- ADC transfer and reset.
- PIO trigger capture.
- frame acquisition.
- capture combination.
- transport queue and record encoding.
- SD storage.
- command parsing.

Keep timing-critical functions and their state in the same translation unit.
Avoid abstraction layers around GPIO and PIO operations because their timing is
part of the design.

### 6. Establish one record implementation on the host

Stack record constants and parsing appear in `device.py` and `read_stk.py` while
export metadata is assembled separately in `export.py`. Move record layout,
checksum, codec, and parsed record types into `spectrometer/records.py`. Make the
serial device and card reader call that module.

The C++ and Python implementations must remain separate but should share a short
versioned format specification and common test vectors.

### 7. Replace hidden compatibility state

The new FPS and integration fields still use a hidden `rate_mode` variable to
support earlier test and method contracts. Migrate callers to explicit
`set_fps()` and `set_integration_ms()` methods then remove `rate_mode`,
`_sync_rate_fields()`, and `_update_exposure_label()`.

### 8. Separate console records from presentation

Console output now retains typed records and applies category filters during
rendering. Move the category mapping and bounded record buffer into a small
`ConsoleModel`. This permits unit tests without constructing Tk widgets and keeps
device message classification outside layout code.

### 9. Reduce comments in executable files

The repository contains valuable failure history. Much of it is duplicated
between source comments, `ARCHITECTURE.md`, `APPLICATION.md`, and `ISSUES.md`.
Keep comments that state timing constraints, invariants, wire layouts, and
non-obvious hardware behavior. Move chronological investigation narratives to
`ISSUES.md`. This reduces scan time without discarding evidence.

### 10. Normalize tests

The test scripts are effective integration checks but execute at import time and
share generated build and settings state. Convert them to pytest functions with
temporary directories and fixtures for the Tk root, stub device, native harness,
and settings path. This removes profile-dependent failures and permits focused
test selection.

## Recommended order

1. Complete firmware state ownership.
2. Extract host record parsing.
3. Remove or replace `live_view.py`.
4. Extract the console and plot renderers from `app.py`.
5. Consolidate capture paths.
6. Split `main.cpp` after the existing tests cover each boundary.
7. Convert the test runner to pytest.

Preserve the current protocol tests and hardware timing constraints during every
step. Simplicity is reduced state ownership and fewer implementations rather than
fewer safeguards.
