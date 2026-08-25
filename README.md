# ArduPilot Telemetry Overlay

Burns telemetry from an ArduPlane DataFlash log (`.bin`) onto flight video as an
FPV-style HUD/OSD: airspeed, ground speed, altitude, height above ground, vertical
speed, battery voltage/current/consumption, throttle, autopilot status messages and an
artificial horizon.

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
- the line `valid log delays run from 171.7 to 415.9` — any log delay outside that range would
  place part of the clip outside the log, so it is wrong by definition.

Also check that every channel you care about was found, and that an encoder is marked
`yes`. On a machine without an NVIDIA GPU only the software encoders will be available;
that is fine, just slower.

### Step 2 — get a rough log delay

The log delay is the log time that corresponds to **video time zero**. Do not compute it
from file timestamps: the camera clock is unreliable. Instead pick one moment you can
see in the video *and* find in the log — the start of the takeoff roll is the easiest —
and give both timestamps with `--anchor-video-time`/`--anchor-log-time`; the tool does
the subtraction for you. Say it happens 40 s into the video, and the log shows the AUTO
takeoff at 215 s: `--anchor-video-time 40 --anchor-log-time 215` (log delay `175`,
printed back so you can check it).

If you cannot spot such an event, let `autosync` propose a starting point — it is a
suggestion, never the answer. It never writes anything unless you pass `--write`, so
there is no "with/without saving" distinction here — a plain run and a `--write` run are
shown in [its reference entry](#autosync--suggest-a-log-delay-and-clock-drift-scale):

```bash
telemetry-overlay autosync flight.MP4 flight.bin --search-min 171.7 --search-max 415.9
```

```powershell
telemetry-overlay autosync "data\ThumbPW_0024.MP4" "data\2026-08-23 10-23-27.bin" `
    --search-min 171.7 --search-max 415.9
```

It spreads several windows across the whole video by default, bound the search with the
log delay range `probe` printed, and fits both a log delay and a drift scale through the
windows it trusts — printing a table so you can see which ones. Read the confidence
verdict: a low one means the footage did not correlate, not that the number is nearly
right; if a window with real manoeuvres in it is still discarded, `--window-length` (more
seconds per window) usually fixes it.

### Step 3 — refine the log delay frame by frame

Render single frames at a moment whose true state you know, and adjust the log delay until
the overlay agrees with the picture. From here on you are nudging a number, not
re-reading two clocks, so switch to `--log-delay` directly — start from the value Step 2
printed. At this point there is nothing saved yet, so `--log-delay` is always explicit:

```bash
telemetry-overlay frame flight.MP4 flight.bin --at 40 --log-delay 175 -o out/f.png
```

```powershell
telemetry-overlay frame "data\ThumbPW_0024.MP4" "data\2026-08-23 10-23-27.bin" `
    --at 40 --log-delay 175 -o out\f.png
```

Look at the frame, then correct. The relationship is worth internalising:

- overlay is **ahead** of the picture (already climbing while the aircraft is still on
  the ground) → **lower** the log delay;
- overlay **lags** the picture → **raise** the log delay.

Counter-intuitive but follows from `log_time = log_delay + video_time * scale`: raising the
log delay makes a *later* log time line up with the *same* video time, so every log event
shifts **earlier** in the video, not later — that is what fixes the overlay lagging.

Iterate in shrinking steps — ±5 s, then ±1 s, then ±0.2 s. The horizon is the most
sensitive check: at a sharp roll, an error of half a second is obvious. Speed and altitude
change too slowly to sync against.

```bash
telemetry-overlay frame flight.MP4 flight.bin --at 40 --log-delay 173 -o out/f.png
telemetry-overlay frame flight.MP4 flight.bin --at 40 --log-delay 171.8 -o out/f.png
```

```powershell
telemetry-overlay frame "data\ThumbPW_0024.MP4" "data\2026-08-23 10-23-27.bin" `
    --at 40 --log-delay 173 -o out\f.png
telemetry-overlay frame "data\ThumbPW_0024.MP4" "data\2026-08-23 10-23-27.bin" `
    --at 40 --log-delay 171.8 -o out\f.png
```

Then verify at a *second*, distant moment — the landing, for instance. If the first point
matches and a point four minutes later does not, the two clocks are drifting, which is
what `scale` is for: re-run `autosync` across a span covering both points, or refine
`--time-scale` by hand at the second point the same way you refined `--log-delay` at the
first.

### Step 4 — save the log delay

Once you are happy, store it beside the video by adding `--save-sync` to the last `frame`
call — no need to run it again separately. Include `--time-scale` too if Step 3 found
drift:

```bash
telemetry-overlay frame flight.MP4 flight.bin --at 40 --log-delay 171.8 --save-sync
```

```powershell
telemetry-overlay frame "data\ThumbPW_0024.MP4" "data\2026-08-23 10-23-27.bin" `
    --at 40 --log-delay 171.8 --save-sync
```

That writes `flight.sync.json` (`data\ThumbPW_0024.sync.json` for the sample — next to
the **video**, regardless of the log's name). If you had used `--anchor-video-time`/
`--anchor-log-time` instead, both timestamps would be saved alongside the computed log
delay, so you can reopen the file later and see what event you synced against. From now
on every command below can either omit `--log-delay` and let it read that file back, or
keep passing `--log-delay` explicitly — useful if you never want to save anything and
prefer to type the number every time (e.g. scripting, or comparing two candidate log
delays side by side). This step is optional: if you skip it, repeat `--log-delay 171.8`
on every command in steps 5-7 below.

### Step 5 — adjust the look

Copy the preset and edit the copy, checking each change with `frame` (about a second per
render, no export needed):

```bash
cp presets/default.json presets/mine.json
```

Without a saved log delay:

```bash
telemetry-overlay frame flight.MP4 flight.bin --at 120 --log-delay 171.8 -p presets/mine.json -o out/f.png
```

```powershell
telemetry-overlay frame "data\ThumbPW_0024.MP4" "data\2026-08-23 10-23-27.bin" `
    --at 120 --log-delay 171.8 -p presets\mine.json -o out\f.png
```

With the log delay saved in step 4 (omit `--log-delay`, it is read from `.sync.json`):

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

Without a saved log delay:

```bash
telemetry-overlay export flight.MP4 flight.bin --log-delay 171.8 -p presets/mine.json \
    --from 210 --to 220 -o out/test.mp4
```

```powershell
telemetry-overlay export "data\ThumbPW_0024.MP4" "data\2026-08-23 10-23-27.bin" `
    --log-delay 171.8 -p presets\mine.json --from 210 --to 220 -o out\test.mp4
```

With the log delay saved in step 4:

```bash
telemetry-overlay export flight.MP4 flight.bin -p presets/mine.json \
    --from 210 --to 220 -o out/test.mp4
```

```powershell
telemetry-overlay export "data\ThumbPW_0024.MP4" "data\2026-08-23 10-23-27.bin" `
    -p presets\mine.json --from 210 --to 220 -o out\test.mp4
```

`--from` and `--to` are in video seconds. Watch it: check the sync at speed, that
the audio is in place, and that nothing is clipped at the frame edges.

### Step 7 — export the whole flight

Without a saved log delay:

```bash
telemetry-overlay export flight.MP4 flight.bin --log-delay 171.8 -p presets/mine.json -o flight_hud.mp4
```

```powershell
telemetry-overlay export "data\ThumbPW_0024.MP4" "data\2026-08-23 10-23-27.bin" `
    --log-delay 171.8 -p presets\mine.json -o flight_hud.mp4
```

With the log delay saved in step 4:

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
- `--log-delay SECONDS` — log time matching video time zero, given directly (`frame` and
  `export` only; `autosync` computes it instead of taking it). Defaults to the value
  stored in the video's `.sync.json`, or `0` if there is none.
- `--anchor-video-time SECONDS` / `--anchor-log-time SECONDS` — an alternative to
  `--log-delay`, given as a pair: the video and log timestamps of one moment you
  recognise in both (e.g. takeoff). The log delay is computed from the two instead of
  you subtracting them by hand. Give either `--log-delay` or this pair, not both.
- `--time-scale FACTOR` — clock-drift scale: `log_time = log_delay + video_time *
  scale`. Advanced, like `--log-delay`; combines with either `--log-delay` or the
  `--anchor-*` pair. Defaults to `1.0`, or to the value stored in `.sync.json` (e.g. from
  `autosync --write`). Not to be confused with `export`'s `--scale`, which downscales the
  output frame.
- `--save-sync` — write the log delay and scale in effect (whether given on the command
  line or read from an existing `.sync.json`) to `<video>.sync.json` (`frame` and
  `export` only). If it came from `--anchor-video-time`/`--anchor-log-time`, both
  timestamps are saved too.

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
telemetry-overlay frame <video> <log> [-p PRESET] [--log-delay SECONDS] [--time-scale FACTOR] [--save-sync]
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
telemetry-overlay frame flight.MP4 flight.bin --at 42 --log-delay 250 -o out/f.png
telemetry-overlay frame flight.MP4 flight.bin --at 42 --overlay-only   # HUD alone
telemetry-overlay frame flight.MP4 flight.bin --at 42 --width 960 -o out/preview.png
```

Against the sample data, using the log delay found in the [tutorial](#step-4--save-the-log-delay):

```powershell
telemetry-overlay frame "data\ThumbPW_0024.MP4" "data\2026-08-23 10-23-27.bin" `
    --at 40 --log-delay 171.8 -o out\f.png
```

### `autosync` — suggest a log delay and clock-drift scale

Measures how fast the image rotates (optical flow) and correlates it with the roll rate
in the log. It runs the optical flow once across the whole span, picks the windows where
the image actually rotates the most (calm cruise or straight legs cannot correlate
against anything, so there is no point spending a window on them), correlates each
against the log, and fits a log delay *and* a scale through the windows that agree with
each other (clocks drift, so a single offset that matches at one point in the video can
be seconds off at another). Prints the result with a confidence verdict and changes
nothing unless you pass `--write`.

```
telemetry-overlay autosync <video> <log> [-p PRESET]
    [--from SECONDS] [--to SECONDS] [--windows N] [--window-length SECONDS]
    [--search-min SECONDS] [--search-max SECONDS] [--write] [--plot DIR]
```

- `video`, `log` — required positionals.
- `-p`, `--preset PATH` — accepted for consistency with the other commands but unused by
  the estimate itself.
- `--from SECONDS` — video time to start spreading windows from (default `0`).
- `--to SECONDS` — video time to stop spreading windows at (default: end of video).
- `--windows N` — number of analysis windows, picked from wherever the image rotates the
  most within `--from`/`--to`, spaced at least one window apart (default `6`). Each
  window is scored independently and only the ones that agree with each other (a
  distinct correlation peak, and a consistent log-delay-vs-time line — see
  [Synchronising video and telemetry](#synchronising-video-and-telemetry)) feed the fit;
  a window that is active but matched the wrong manoeuvre is excluded, not averaged in.
  `--windows 1` disables the scale fit, analyses exactly `--from`→`--to` as one slice,
  and keeps `scale` at `1.0` — the old single-slice behaviour.
- `--window-length SECONDS` — duration of each window (default `20`). Longer windows
  correlate more reliably but cost more decode time; the sample video needed `40` to get
  a confident peak per window (see below).
- `--search-min` / `--search-max SECONDS` — bound the log delay the estimate is allowed to
  return, in **log** seconds (the same quantity `probe` reports as "valid log delays"),
  applied to every window.
- `--write` — store the accepted estimate (log delay **and** scale) in `<video>.sync.json`.
  Without it, `autosync` only prints the suggestion; nothing is ever written automatically.
- `--plot DIR` — with more than one window, save `autosync_fit.png` (each window's log
  delay against its position in the video, trustworthy vs. discarded, and the fitted
  line — the drift shows up directly as its slope) plus `autosync_diagnostics.png`
  (roll and roll-rate overlay for the *whole* analysed `--from`→`--to` span, from the
  single continuous optical-flow pass — not just the best-scoring window). With
  `--windows 1`, only the latter is produced.

Needs visible, textured ground and real manoeuvring. Footage of empty sky, straight and
level flight, or a gimballed camera will not correlate in any window; that is what the
per-window and overall confidence verdicts are for. Always verify with `frame` at more
than one point in the video before trusting the result.

```bash
telemetry-overlay autosync flight.MP4 flight.bin --search-min 170 --search-max 420
telemetry-overlay autosync flight.MP4 flight.bin --search-min 170 --search-max 420 --write
```

Against the sample data, using the log window `probe` reported (`171.7` → `415.9`): the
default 20 s windows were too short for a confident peak on this footage, `40` s worked —
```powershell
telemetry-overlay autosync "data\ThumbPW_0024.MP4" "data\2026-08-23 10-23-27.bin" `
    --window-length 40 --search-min 171.7 --search-max 415.9 --plot out
```
found `log_delay 414.057s`, `scale 1.00137` from 3 windows (correlation up to `0.98`)
spanning 168 s — consistent with the manually-found `414.130` around video time 100 s,
and with the drift that made a fixed offset lose sync by video time 200 s.

Not every clip correlates this cleanly. On footage with a less distinctive roll signal
(similar turns repeated throughout, or long calm stretches), the windows may not agree
closely enough to pass the fit's checks even after several tries with different
`--window-length`/`--windows` values — `autosync` will say so (`check it by eye`) rather
than guess. That is expected, not a bug: fall back to
[anchoring by eye](#step-2--get-a-rough-log-delay) at more than one point in the video.

### `manualsync` — check a chosen log delay by eye

Plots a specific video slice's optical-flow roll rate against the log's roll and roll
rate, shifted by a log delay you already picked (by hand, or from `autosync`). Unlike
`autosync` it does not search for anything; it just draws the two panel diagnostic plot
for that one slice so you can see whether the two traces actually line up.

```
telemetry-overlay manualsync <video> <log> [-p PRESET]
    [--from SECONDS] [--to SECONDS] (--log-delay SECONDS | --anchor-video-time SECONDS --anchor-log-time SECONDS)
    [--time-scale FACTOR] [--plot DIR]
```

- `video`, `log` — required positionals.
- `-p`, `--preset PATH` — accepted for consistency with the other commands but unused.
- `--from` / `--to SECONDS` — the video slice to analyse, in video seconds (default: the
  whole video, `0` to its end).
- `--log-delay SECONDS` — the log delay to check: `log_time = log_delay + video_time * scale`.
- `--anchor-video-time SECONDS` / `--anchor-log-time SECONDS` — an alternative to
  `--log-delay`: the two timestamps of one moment recognisable in both video and log.
  Give one form or the other; there is no `.sync.json` fallback here, unlike `frame` and
  `export`.
- `--time-scale FACTOR` — the clock-drift scale to check alongside either form above
  (default `1.0`).
- `--plot DIR` — directory to save `manualsync_diagnostics.png` in (default: `out`).

```powershell
telemetry-overlay manualsync "data\ThumbPW_0024.MP4" "data\2026-08-23 10-23-27.bin" `
    --from 60 --to 90 --log-delay 206.1
```

### `export` — write the final video

```
telemetry-overlay export <video> <log> [-p PRESET]
    [--log-delay SECONDS | --anchor-video-time SECONDS --anchor-log-time SECONDS] [--time-scale FACTOR] [--save-sync]
    [-o PATH] [--from SECONDS] [--to SECONDS]
    [--encoder KEY] [--quality N] [--scale FACTOR] [--no-audio] [-y]
```

- `video`, `log` — required positionals.
- `-o`, `--output PATH` — where to write the file (default: `out/<video stem>.overlay.mp4`).
- `--from SECONDS` — trim start, in video seconds (default: from the beginning).
- `--to SECONDS` — trim end, in video seconds (default: to the end of the video).
- `--encoder KEY` — one of `nvenc_h264`, `nvenc_hevc`, `x264`, `x265`; see `probe` for
  which of these this machine actually supports. Default: the best available, NVENC
  first, falling back to x264.
- `--quality N` — CQ (NVENC) or CRF (x264/x265) value; lower is higher quality and a
  larger file. Default depends on the chosen encoder.
- `--scale FACTOR` — downscale the output by this factor (e.g. `0.5` for half
  resolution) for a fast draft export while iterating on a preset or a sync log delay. The
  HUD is unaffected: element geometry is frame-relative, so it renders correctly at any
  size. Default: 1.0, the source resolution.
- `--no-audio` — drop the audio track instead of copying it untouched.
- `-y`, `--overwrite` — overwrite `--output` if it already exists; otherwise `export`
  refuses to clobber an existing file.

```bash
# 10-second test segment first: seconds instead of minutes, and half resolution for speed
telemetry-overlay export flight.MP4 flight.bin --log-delay 250 --from 100 --to 110 \
    --scale 0.5

# then the whole clip at full resolution, with an explicit encoder and quality
telemetry-overlay export flight.MP4 flight.bin --log-delay 250 \
    --encoder x264 --quality 20 -o flight_hud.mp4 -y
```

Against the sample data, a 10-second test segment starting at the AUTO takeoff:

```powershell
telemetry-overlay export "data\ThumbPW_0024.MP4" "data\2026-08-23 10-23-27.bin" `
    --log-delay 171.8 --from 40 --to 50 -o out\test.mp4
```

## Synchronising video and telemetry

The camera clock cannot be trusted — on real footage the MP4 creation time can be off by
more than a year — so the alignment is an explicit **log delay**: the log time, in seconds,
that corresponds to video time zero. A **scale** factor corrects for clock drift between
the camera and the flight controller, which on a clip of a few minutes can add up to a
noticeable desync by the end even when the start lines up perfectly:

```
log_time = log_delay + video_time * scale
```

`scale` defaults to `1.0` (no drift) and, for most flights, stays close to it.

**Raising the log delay moves the telemetry later in the log for the same point in the
video** — video time zero is matched against a log time further into the flight, so
what the overlay shows advances (it looks *ahead* of the picture, e.g. already climbing
while the aircraft is still on the ground on screen). **Lowering the log delay** matches
video time zero against an earlier log time, so the overlay shows something that
happened earlier (it *lags* the picture). To fix a mismatch: overlay ahead of the
picture → lower the log delay; overlay lagging the picture → raise it.

The easiest way to find the log delay: read off the timestamp of a recognisable event
(the moment of rotation on takeoff, a sharp roll, touchdown) both from the video and from
the log, and give the pair to `--anchor-video-time`/`--anchor-log-time` — the log delay
is computed for you, no mental subtraction required. `probe` prints the range of log
delays for which the clip fits inside the log, useful as a sanity check on the result.
From there, refine by rendering frames and nudging the log delay directly with
`--log-delay` until the overlay matches the picture exactly (see
[Step 3](#step-3--refine-the-log-delay-frame-by-frame) of the tutorial).

Scale needs two points, far apart in the video, to be measurable at all — a single
anchor cannot tell drift from a plain offset. [`autosync`](#autosync--suggest-a-log-delay-and-clock-drift-scale)
does this for you: it correlates several windows spread across the video and fits both
log delay and scale through the ones it trusts. If you match a log delay by eye at one
point in the video and the overlay is still off at another point several minutes away,
that mismatch is what `scale` is for — either run `autosync` across both points, or set
`--time-scale` directly if you already have a number.

Store the result next to the video with `--save-sync`, which writes `flight.sync.json`
(with the anchor pair alongside the log delay, if that is what you used, so the file
stays readable and re-editable); later commands pick it up automatically when
`--log-delay`, `--anchor-*` and `--time-scale` are all omitted.

The log delay lives apart from the preset on purpose: a preset describes a *look* and is
reused across flights, while a log delay belongs to one video/log pair.

## Presets

`presets/default.json` holds the layout, the theme and the units. Positions are
normalised to the frame (0..1) and all sizes are fractions of frame height, so a preset
made against a 1080p proxy renders identically on a 4K master.

Element types: `readout` (any telemetry channel), `heading`, `mode`, `timer`, `horizon`,
`messages`. Each entry takes `x`, `y`, `anchor`, `visible`, `scale`, `unit`, `decimals`
and a type-specific `options` block — see the docstrings in
`src/telemetry_overlay/hud/elements/`.

Units are per element: `km/h`, `kt`, `mph`, `m/s` for speeds, `m`/`ft` for heights,
`m/s_v`/`ft/min` for vertical speed, `A` for current, `mAh` for consumed charge, `%` for
throttle.

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
| Current | `BAT.Curr` | — |
| Consumed | `BAT.CurrTot` (already mAh, cumulative) | — |
| Throttle | `CTUN.ThO` (already 0..100 %) | — |
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
cross-correlation against signals with a known log delay.
