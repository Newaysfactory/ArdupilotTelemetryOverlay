# ArduPilot Telemetry Overlay

Burns telemetry from an ArduPlane DataFlash log (`.bin`) onto flight video as an
FPV-style HUD/OSD: airspeed, ground speed, altitude, height above ground, vertical
speed, battery voltage, autopilot status messages and an artificial horizon.

Phase 1 (this version) is the command-line core. The interactive GUI for time
synchronisation and drag-and-drop layout editing is phase 2; the rendering engine,
preset format and export pipeline are already shared with it.

## Setup

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows
# or: .venv/bin/pip install -r requirements.txt
```

No external FFmpeg installation is needed: PyAV bundles a complete FFmpeg build,
including the NVENC hardware encoders.

Optionally install the package to get the `telemetry-overlay` command on your PATH:

```bash
.venv/Scripts/python.exe -m pip install -e .
```

Without installing, use `python -m telemetry_overlay` with `PYTHONPATH=src`.

## Commands

### `probe` — see what you have

Start here. Reports the video's exact frame rate and duration, which encoders this
machine can use, and every telemetry channel found in the log with its rate, validity
and range.

```bash
telemetry-overlay probe flight.MP4 flight.bin
```

### `frame` — iterate on the look

Renders one composited frame to a PNG in about a second. This is the fast loop for
adjusting a preset, with no GUI and no waiting for an export.

```bash
telemetry-overlay frame flight.MP4 flight.bin --at 42 --offset 250 -o out/f.png
telemetry-overlay frame flight.MP4 flight.bin --at 42 --overlay-only   # HUD alone
```

### `autosync` — suggest a time offset

Measures how fast the image rotates (optical flow) and correlates it with the roll rate
in the log. It prints an estimate and a confidence score and changes nothing unless you
pass `--write`.

It does not analyse the whole video: it takes a single slice of it and slides that slice
against the log. Two flags choose the slice, both in **video** seconds counted from the
start of the file:

- `--from` — where the slice begins (default `0`, the very first frame)
- `--window` — how long the slice lasts (default `60`)

So `--from 30 --window 60` analyses video time 30 s → 90 s. Pick a stretch with actual
turns in it: skip the taxi and the climb-out, and keep the window long enough to contain
several manoeuvres.

```bash
# analyse the 60 seconds of video that start 30 seconds in
telemetry-overlay autosync flight.MP4 flight.bin --from 30 --window 60
```

Two more flags restrict the answer instead of the input. `--search-min` and
`--search-max` bound the offset it is allowed to return, in **log** seconds — useful
when `probe` already told you the log window, since an offset outside it cannot be right:

```bash
telemetry-overlay autosync flight.MP4 flight.bin --from 30 --window 60 \
    --search-min 170 --search-max 420
```

Add `--write` to store the accepted estimate in `<video>.sync.json`.

It needs visible, textured ground and real manoeuvring. Footage of empty sky, straight
and level flight, or a gimballed camera will not correlate; that is what the confidence
score is for. Always verify with `frame` before trusting it.

### `export` — write the final video

```bash
# 10-second test segment first: seconds instead of minutes
telemetry-overlay export flight.MP4 flight.bin --offset 250 --start 100 --duration 10

# then the whole clip
telemetry-overlay export flight.MP4 flight.bin --offset 250 -o flight_hud.mp4
```

Useful flags: `--encoder nvenc_h264|nvenc_hevc|x264|x265`, `--quality` (CQ/CRF, lower is
better), `--no-audio`, `-y` to overwrite.

## Synchronising video and telemetry

The camera clock cannot be trusted — on real footage the MP4 creation time can be off by
more than a year — so the alignment is an explicit **offset**: the log time, in seconds,
that corresponds to video time zero.

```
log_time = offset + video_time
```

Find it by rendering frames around a recognisable event (the moment of rotation on
takeoff, a sharp roll, touchdown) and adjusting until the overlay matches the picture.
`probe` prints the range of offsets for which the clip fits inside the log.

Store it next to the video with `--save-sync`, which writes `flight.sync.json`; later
commands pick it up automatically when `--offset` is omitted.

The offset lives apart from the preset on purpose: a preset describes a *look* and is
reused across flights, while an offset belongs to one video/log pair.

## Presets

`presets/default.json` holds the layout, the theme and the units. Positions are
normalised to the frame (0..1) and all sizes are fractions of frame height, so a preset
made against a 1080p proxy renders identically on a 4K master.

Element types: `readout` (any telemetry channel), `heading`, `mode`, `timer`, `horizon`,
`messages`. Each entry takes `x`, `y`, `anchor`, `visible`, `scale`, `unit`, `decimals`
and a type-specific `options` block — see the docstrings in
`src/telemetry_overlay/hud/elements/`.

Units are per element: `km/h`, `kt`, `mph`, `m/s` for speeds, `m`/`ft` for heights,
`m/s_v`/`ft/min` for vertical speed.

Two options worth knowing:

- `warn_below` / `warn_above` on a readout switch it to the theme's warning colour.
  Thresholds are in SI units (V, m, m/s) whatever the display unit.
- `pitch_offset` / `roll_offset` on the horizon are camera mount trim, in degrees, to
  line the artificial horizon up with the real one when the camera does not point along
  the flight path.

## Telemetry sources

Verified against ArduPlane 4.7. `probe` shows which source each channel actually used.

| Channel | Source | Fallback |
|---|---|---|
| IAS | `ARSP.Airspeed`, gated on the health flag | `CTUN.As` |
| GS | `hypot(XKF1.VN, XKF1.VE)` — EKF velocity, far smoother than GPS | `GPS.Spd` |
| ALT | `BARO.AltAMSL` (absolute) | `GPS.Alt`, `POS.Alt` |
| AGL | `RFND.Dist`, only while the rangefinder reports Good | none, by design |
| VS | `BARO.CRt` | `-XKF1.VD` |
| Voltage | `BAT.Volt` | — |
| Attitude | `ATT.Roll/Pitch/Yaw` | `AHR2` |
| Mode, messages | `MODE`, `MSG` | — |

**AGL shows `NO AGL` most of the time, and that is correct.** A rangefinder is a
short-range sensor — around 12 m on the test aircraft — so it only reads near the
ground, during takeoff, landing and low passes. There is deliberately no fallback to
barometric or terrain height: AGL here means a measured height. A hysteresis filter
(default 0.3 s) keeps the readout from strobing at the edge of the sensor's range.

Status messages are guaranteed at least one second on screen each. When several arrive
in the same millisecond they queue rather than flash by. The boot banner and everything
logged before arming is hidden by default.

Parsed logs are cached beside the `.bin` as `.overlay-cache.npz`, so only the first read
costs anything. The cache invalidates itself when the log changes or when the channel
definitions in `fields.py` are edited.

## About "without re-encoding"

Burning pixels into a picture requires decoding, compositing and encoding the video
stream: there is no way to avoid re-encoding it. What this tool does instead is keep the
loss negligible and the process fast:

- constant-quality encoding (NVENC CQ or x264 CRF) at the original resolution and exact
  frame rate, preserving the source's colour range and matrix;
- the **audio stream is copied packet by packet**, never re-encoded;
- Python never touches the video's pixels. It draws only the HUD, into the handful of
  rectangular *bands* the layout can reach (typically a quarter of the frame area);
  decoding, compositing and encoding all happen in FFmpeg's C code inside the same
  process, with no frame pipe between processes.

If you want the original file left untouched, the alternative is to composite in a video
editor. Exporting the HUD alone to a file with an alpha channel is not implemented yet;
`frame --overlay-only` produces single transparent PNGs today.

## Tests

```bash
.venv/Scripts/python.exe -m pytest tests/ -q
```

The suite covers interpolation at the edges of the log window, gap handling, the
rangefinder validity hysteresis, the message queue (using timings taken from a real
log), unit conversions, preset round-trips, band geometry, and the sync
cross-correlation against signals with a known offset.
