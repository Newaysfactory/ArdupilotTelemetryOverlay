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

## How to run the command

There is no `telemetry-overlay` file in the project root: it is a console script that
only exists once the package is installed into the virtualenv. Pick one of the two forms
below — every example in this README writes `telemetry-overlay`, and you substitute
whichever you chose.

**Installed (recommended).** Install the package once, in editable mode, and the command
becomes available inside the virtualenv:

```bash
.venv/Scripts/python.exe -m pip install -e .      # Windows
# or: .venv/bin/pip install -e .
```

Then, from the project root:

```powershell
.venv\Scripts\telemetry-overlay.exe probe "data\ThumbPW_0024.MP4" "data\2026-08-23 10-23-27.bin"
```

Activating the venv (`.venv\Scripts\Activate.ps1`, or `source .venv/bin/activate`) lets
you drop the path and type `telemetry-overlay` directly.

**Without installing.** Run the package as a module, telling Python where the sources
are. From the project root:

```powershell
$env:PYTHONPATH = "src"
.venv\Scripts\python.exe -m telemetry_overlay probe "data\ThumbPW_0024.MP4" "data\2026-08-23 10-23-27.bin"
```

```bash
# bash / Linux / macOS equivalent
PYTHONPATH=src .venv/bin/python -m telemetry_overlay probe data/flight.MP4 data/flight.bin
```

`$env:PYTHONPATH` lasts for the current shell session only, so set it once per terminal.
Note the quotes: paths containing spaces — like the sample log — need them.

## Tutorial: from a `.bin` and a video to a finished clip

A full pass, in order. The numbers below are from the sample flight in `data/`; substitute
your own. Every command is idempotent, so repeat any step as often as you like.

The examples are written as `telemetry-overlay <args>` for readability. That is not a file
in the project root — replace it with whichever invocation you picked in
[How to run the command](#how-to-run-the-command), i.e. either
`.venv\Scripts\telemetry-overlay.exe` or `.venv\Scripts\python.exe -m telemetry_overlay`
with `PYTHONPATH=src` set.

Each step below gives the portable command first (`flight.MP4`/`flight.bin` standing in
for your own files), then the same command run against the sample pair in `data/`.

### Step 1 — look at both files

```bash
telemetry-overlay probe flight.MP4 flight.bin
```

```powershell
telemetry-overlay probe "data\ThumbPW_0024.MP4" "data\2026-08-23 10-23-27.bin"
```

Write down three things from the output:

- the **video duration** (sample: `224.8s`);
- the **log window**, in flight-controller seconds (sample: `171.7s -> 640.6s`) and the
  arming time (`181.8s`);
- the line `valid offsets run from 171.7 to 415.9` — any offset outside that range would
  place part of the clip outside the log, so it is wrong by definition.

Also check that every channel you care about was found, and that an encoder is marked
`yes`. On a machine without an NVIDIA GPU only the software encoders will be available;
that is fine, just slower.

### Step 2 — get a rough offset

The offset is the log time that corresponds to **video time zero**. Do not compute it
from file timestamps: the camera clock is unreliable. Instead pick one event you can see
in the video *and* find in the log, and subtract.

The easiest event is the start of the takeoff roll. Say it happens 40 s into the video,
and the log shows the AUTO takeoff at 215 s:

```
offset = 215 - 40 = 175
```

If you cannot spot such an event, let `autosync` propose a starting point — it is a
suggestion, never the answer. It never writes anything unless you pass `--write`, so
there is no "with/without saving" distinction here — a plain run and a `--write` run are
shown in [its reference entry](#autosync--suggest-a-time-offset):

```bash
telemetry-overlay autosync flight.MP4 flight.bin --from 60 --window 60 \
    --search-min 171.7 --search-max 415.9
```

```powershell
telemetry-overlay autosync "data\ThumbPW_0024.MP4" "data\2026-08-23 10-23-27.bin" `
    --from 60 --window 60 --search-min 171.7 --search-max 415.9
```

Pass `--from`/`--window` a stretch of video with real turns in it, and bound the search
with the offset range `probe` printed. Read the confidence score: a low one means the
footage did not correlate, not that the number is nearly right.

### Step 3 — refine the offset frame by frame

Render single frames at a moment whose true state you know, and adjust the offset until
the overlay agrees with the picture. At this point there is nothing saved yet, so
`--offset` is always explicit:

```bash
telemetry-overlay frame flight.MP4 flight.bin --at 40 --offset 175 -o out/f.png
```

```powershell
telemetry-overlay frame "data\ThumbPW_0024.MP4" "data\2026-08-23 10-23-27.bin" `
    --at 40 --offset 175 -o out\f.png
```

Look at the frame, then correct. The relationship is worth internalising:

- overlay is **ahead** of the picture (already climbing while the aircraft is still on
  the ground) → **lower** the offset;
- overlay **lags** the picture → **raise** the offset.

Iterate in shrinking steps — ±5 s, then ±1 s, then ±0.2 s. The horizon is the most
sensitive check: at a sharp roll, an error of half a second is obvious. Speed and altitude
change too slowly to sync against.

```bash
telemetry-overlay frame flight.MP4 flight.bin --at 40 --offset 173 -o out/f.png
telemetry-overlay frame flight.MP4 flight.bin --at 40 --offset 171.8 -o out/f.png
```

```powershell
telemetry-overlay frame "data\ThumbPW_0024.MP4" "data\2026-08-23 10-23-27.bin" `
    --at 40 --offset 173 -o out\f.png
telemetry-overlay frame "data\ThumbPW_0024.MP4" "data\2026-08-23 10-23-27.bin" `
    --at 40 --offset 171.8 -o out\f.png
```

Then verify at a *second*, distant moment — the landing, for instance. If the first point
matches and a point four minutes later does not, the two clocks are drifting, which is
what the `scale` field in the sync file is for.

### Step 4 — save the offset

Once you are happy, store it beside the video by adding `--save-sync` to the last `frame`
call — no need to run it again separately:

```bash
telemetry-overlay frame flight.MP4 flight.bin --at 40 --offset 171.8 --save-sync
```

```powershell
telemetry-overlay frame "data\ThumbPW_0024.MP4" "data\2026-08-23 10-23-27.bin" `
    --at 40 --offset 171.8 --save-sync
```

That writes `flight.sync.json` (`data\ThumbPW_0024.sync.json` for the sample — next to
the **video**, regardless of the log's name). From now on every command below can either
omit `--offset` and let it read that file back, or keep passing `--offset` explicitly —
useful if you never want to save anything and prefer to type the number every time (e.g.
scripting, or comparing two candidate offsets side by side). This step is optional: if
you skip it, repeat `--offset 171.8` on every command in steps 5-7 below.

### Step 5 — adjust the look

Copy the preset and edit the copy, checking each change with `frame` (about a second per
render, no export needed):

```bash
cp presets/default.json presets/mine.json
```

Without a saved offset:

```bash
telemetry-overlay frame flight.MP4 flight.bin --at 120 --offset 171.8 -p presets/mine.json -o out/f.png
```

```powershell
telemetry-overlay frame "data\ThumbPW_0024.MP4" "data\2026-08-23 10-23-27.bin" `
    --at 120 --offset 171.8 -p presets\mine.json -o out\f.png
```

With the offset saved in step 4 (omit `--offset`, it is read from `.sync.json`):

```bash
telemetry-overlay frame flight.MP4 flight.bin --at 120 -p presets/mine.json -o out/f.png
```

```powershell
telemetry-overlay frame "data\ThumbPW_0024.MP4" "data\2026-08-23 10-23-27.bin" `
    --at 120 -p presets\mine.json -o out\f.png
```

Positions are normalised (0..1) and sizes are fractions of frame height, so what you tune
here holds at any resolution. See [Presets](#presets) for the fields. A good frame to
work on is one where the aircraft is banked and messages are on screen.

### Step 6 — export a short test segment

Never go straight to the full clip. Render ten seconds first.

Without a saved offset:

```bash
telemetry-overlay export flight.MP4 flight.bin --offset 171.8 -p presets/mine.json \
    --start 210 --duration 10 -o out/test.mp4
```

```powershell
telemetry-overlay export "data\ThumbPW_0024.MP4" "data\2026-08-23 10-23-27.bin" `
    --offset 171.8 -p presets\mine.json --start 210 --duration 10 -o out\test.mp4
```

With the offset saved in step 4:

```bash
telemetry-overlay export flight.MP4 flight.bin -p presets/mine.json \
    --start 210 --duration 10 -o out/test.mp4
```

```powershell
telemetry-overlay export "data\ThumbPW_0024.MP4" "data\2026-08-23 10-23-27.bin" `
    -p presets\mine.json --start 210 --duration 10 -o out\test.mp4
```

`--start` and `--duration` are in video seconds. Watch it: check the sync at speed, that
the audio is in place, and that nothing is clipped at the frame edges.

### Step 7 — export the whole flight

Without a saved offset:

```bash
telemetry-overlay export flight.MP4 flight.bin --offset 171.8 -p presets/mine.json -o flight_hud.mp4
```

```powershell
telemetry-overlay export "data\ThumbPW_0024.MP4" "data\2026-08-23 10-23-27.bin" `
    --offset 171.8 -p presets\mine.json -o flight_hud.mp4
```

With the offset saved in step 4:

```bash
telemetry-overlay export flight.MP4 flight.bin -p presets/mine.json -o flight_hud.mp4
```

```powershell
telemetry-overlay export "data\ThumbPW_0024.MP4" "data\2026-08-23 10-23-27.bin" `
    -p presets\mine.json -o flight_hud.mp4
```

Expect roughly 9 fps at 4K with the software encoder — about 12 minutes for a 4-minute
clip — and far less with NVENC. Add `--encoder`/`--quality` to override the automatic
choice, and `-y` to overwrite an existing output.

## Commands

Reference for each subcommand; the tutorial above is the guided path. Two global flags
sit before the subcommand name: `--version` prints the package version, and
`-v`/`--verbose` (repeatable, e.g. `-vv`) raises the logging level.

`frame`, `autosync` and `export` all share two arguments, in addition to the positional
`video` and `log`:

- `-p`, `--preset PATH` — the layout/theme/units file to render with (default:
  `presets/default.json`). See [Presets](#presets).
- `--offset SECONDS` — log time matching video time zero (`frame` and `export` only;
  `autosync` computes it instead of taking it). Defaults to the value stored in the
  video's `.sync.json`, or `0` if there is none.
- `--save-sync` — write the offset in effect (whether given on the command line or read
  from an existing `.sync.json`) to `<video>.sync.json` (`frame` and `export` only).

### `probe` — see what you have

Start here. Reports the video's exact frame rate and duration, which encoders this
machine can use, and every telemetry channel found in the log with its rate, validity
and range.

```
telemetry-overlay probe <video> [log]
```

- `video` — path to the video file (required).
- `log` — path to the `.bin` DataFlash log (optional). Without it, `probe` reports only
  the video and the available encoders, skipping the channel/mode/message report.

```bash
telemetry-overlay probe flight.MP4 flight.bin
telemetry-overlay probe flight.MP4               # video and encoders only
```

Against the sample data, in PowerShell from the project root:

```powershell
telemetry-overlay probe "data\ThumbPW_0024.MP4" "data\2026-08-23 10-23-27.bin"
```

### `frame` — iterate on the look

Renders one composited frame to a PNG in about a second. This is the fast loop for
adjusting a preset, with no GUI and no waiting for an export.

```
telemetry-overlay frame <video> <log> [-p PRESET] [--offset SECONDS] [--save-sync]
    [--at SECONDS] [-o PATH] [--width PIXELS] [--overlay-only]
```

- `video`, `log` — required positionals.
- `--at SECONDS` — video time to render (default `0`).
- `-o`, `--output PATH` — where to write the PNG (default: `out/frame.png`).
- `--width PIXELS` — downscale the saved image to this width; full resolution by
  default. Handy for a quick look without waiting on a 4K PNG.
- `--overlay-only` — save the transparent HUD alone instead of compositing it onto the
  video frame (an alpha-channel PNG, the same image `--overlay-only` would give you if
  you later wanted to composite in an external editor).

```bash
telemetry-overlay frame flight.MP4 flight.bin --at 42 --offset 250 -o out/f.png
telemetry-overlay frame flight.MP4 flight.bin --at 42 --overlay-only   # HUD alone
telemetry-overlay frame flight.MP4 flight.bin --at 42 --width 960 -o out/preview.png
```

Against the sample data, using the offset found in the [tutorial](#step-4--save-the-offset):

```powershell
telemetry-overlay frame "data\ThumbPW_0024.MP4" "data\2026-08-23 10-23-27.bin" `
    --at 40 --offset 171.8 -o out\f.png
```

### `autosync` — suggest a time offset

Measures how fast the image rotates (optical flow) and correlates it with the roll rate
in the log. It prints an estimate and a confidence score and changes nothing unless you
pass `--write`.

```
telemetry-overlay autosync <video> <log> [-p PRESET]
    [--from SECONDS] [--window SECONDS] [--search-min SECONDS] [--search-max SECONDS]
    [--write] [--plot DIR]
```

- `video`, `log` — required positionals.
- `-p`, `--preset PATH` — accepted for consistency with the other commands but unused by
  the estimate itself.
- `--from SECONDS` — video time the analysed slice starts at (default `0`).
- `--window SECONDS` — how long the slice lasts (default `60`).
- `--search-min` / `--search-max SECONDS` — bound the offset the estimate is allowed to
  return, in **log** seconds.
- `--write` — store the accepted estimate in `<video>.sync.json`. Without it, `autosync`
  only prints the suggestion; nothing is ever written automatically.
- `--plot DIR` — save `autosync_diagnostics.png` to `DIR`: a top panel with the log's
  roll, and a bottom panel overlaying the log's roll rate with the video's optical-flow
  roll rate, the video trace shifted by the estimated offset so the two lines line up
  the way the estimate claims. Use it to see by eye why a correlation is weak — e.g. no
  rotation signal in the chosen video slice, a flat stretch of the log, or two shapes
  that clearly do not match despite the shift.

It does not analyse the whole video: it takes a single slice of it and slides that slice
against the log. `--from`/`--window` choose the slice, both in **video** seconds counted
from the start of the file, e.g. `--from 30 --window 60` analyses video time 30 s → 90 s.
Pick a stretch with actual turns in it: skip the taxi and the climb-out, and keep the
window long enough to contain several manoeuvres.

`--search-min`/`--search-max` restrict the answer instead of the input — useful when
`probe` already told you the log window, since an offset outside it cannot be right:

```bash
telemetry-overlay autosync flight.MP4 flight.bin --from 30 --window 60 \
    --search-min 170 --search-max 420
telemetry-overlay autosync flight.MP4 flight.bin --from 30 --window 60 --write
```

Against the sample data, using the log window `probe` reported (`171.7` → `415.9`):

```powershell
telemetry-overlay autosync "data\ThumbPW_0024.MP4" "data\2026-08-23 10-23-27.bin" `
    --from 60 --window 60 --search-min 171.7 --search-max 415.9
```

It needs visible, textured ground and real manoeuvring. Footage of empty sky, straight
and level flight, or a gimballed camera will not correlate; that is what the confidence
score is for. Always verify with `frame` before trusting it.

### `manualsync` — check a chosen offset by eye

Plots a specific video slice's optical-flow roll rate against the log's roll and roll
rate, shifted by an offset you already picked (by hand, or from `autosync`). Unlike
`autosync` it does not search for anything; it just draws the two panel diagnostic plot
for that one slice so you can see whether the two traces actually line up.

```
telemetry-overlay manualsync <video> <log> [-p PRESET]
    --from SECONDS --to SECONDS --offset SECONDS [--plot DIR]
```

- `video`, `log` — required positionals.
- `-p`, `--preset PATH` — accepted for consistency with the other commands but unused.
- `--from` / `--to SECONDS` — the video slice to analyse, in video seconds.
- `--offset SECONDS` — the offset to check: `log_time = offset + video_time`.
- `--plot DIR` — directory to save `manualsync_diagnostics.png` in (default: `out`).

```powershell
telemetry-overlay manualsync "data\ThumbPW_0024.MP4" "data\2026-08-23 10-23-27.bin" `
    --from 60 --to 90 --offset 206.1
```

### `export` — write the final video

```
telemetry-overlay export <video> <log> [-p PRESET] [--offset SECONDS] [--save-sync]
    [-o PATH] [--start SECONDS] [--duration SECONDS]
    [--encoder KEY] [--quality N] [--no-audio] [-y]
```

- `video`, `log` — required positionals.
- `-o`, `--output PATH` — where to write the file (default: `<video>.overlay.mp4`).
- `--start SECONDS` — trim start, in video seconds (default: from the beginning).
- `--duration SECONDS` — trim duration, in seconds (default: to the end of the video).
- `--encoder KEY` — one of `nvenc_h264`, `nvenc_hevc`, `x264`, `x265`; see `probe` for
  which of these this machine actually supports. Default: the best available, NVENC
  first, falling back to x264.
- `--quality N` — CQ (NVENC) or CRF (x264/x265) value; lower is higher quality and a
  larger file. Default depends on the chosen encoder.
- `--no-audio` — drop the audio track instead of copying it untouched.
- `-y`, `--overwrite` — overwrite `--output` if it already exists; otherwise `export`
  refuses to clobber an existing file.

```bash
# 10-second test segment first: seconds instead of minutes
telemetry-overlay export flight.MP4 flight.bin --offset 250 --start 100 --duration 10

# then the whole clip, with an explicit encoder and quality
telemetry-overlay export flight.MP4 flight.bin --offset 250 \
    --encoder x264 --quality 20 -o flight_hud.mp4 -y
```

Against the sample data, a 10-second test segment starting at the AUTO takeoff:

```powershell
telemetry-overlay export "data\ThumbPW_0024.MP4" "data\2026-08-23 10-23-27.bin" `
    --offset 171.8 --start 40 --duration 10 -o out\test.mp4
```

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
