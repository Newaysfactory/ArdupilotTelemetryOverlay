# ArduPilot Telemetry Overlay

Burns telemetry from an ArduPlane DataFlash log (`.bin`) onto flight video as an
FPV-style HUD/OSD: airspeed, ground speed, altitude, height above ground, vertical
speed, battery voltage/current/consumption, throttle, wind direction and speed,
autopilot status messages and an artificial horizon.

OS tested: Windows 11 and Ubuntu 22.04 LTS
Ardupilot logs tested: ArduPlane 4.7.0
Video files tested: RunCam Thumb Pro 4K

> **Note:** the `notes/` folder is a private git submodule (internal development
> notes, not needed to use or build the program). If you clone this repo without
> access to it, it will simply stay empty -- no impact on building, testing or
> running the app. See [`.gitmodules`](.gitmodules).

## Table of contents

- [Download (no Python required)](#download-no-python-required)
- [Setup](#setup)
- [Installation](#installation)
- [GUI](#gui)
  - [1 · Align](#1--align)
  - [2 · Preview](#2--preview)
  - [3 · Export](#3--export)
- [How to run the command](#how-to-run-the-command)
- [Commands](#commands)
  - [Shared options](#shared-options)
  - [`probe` — see what you have](#probe--see-what-you-have)
  - [`frame` — iterate on the look](#frame--iterate-on-the-look)
  - [`autosync` — suggest a log delay and clock-drift scale](#autosync--suggest-a-log-delay-and-clock-drift-scale)
  - [`manualsync` — check a chosen log delay by eye](#manualsync--check-a-chosen-log-delay-by-eye)
  - [`export` — write the final video](#export--write-the-final-video)
- [Synchronising video and telemetry](#synchronising-video-and-telemetry)
- [Presets](#presets)
- [Telemetry sources](#telemetry-sources)
- [Cache](#cache)
- [Re-encoding notes](#re-encoding-notes)
- [Tests](#tests)

## Download (no Python required)

If you just want to use the GUI and don't want to install Python or any dependency,
grab a prebuilt package from the [Releases page](../../releases) instead of following
the rest of this section:

- **Windows** — download `telemetry-overlay-gui-windows.zip`, extract it anywhere, and
  run `telemetry-overlay-gui.exe` inside the extracted folder.
- **macOS** — download `telemetry-overlay-gui-macos.dmg`, open it, and drag
  `TelemetryOverlay.app` into Applications. The app is not notarised by Apple, so the
  first launch will be blocked by Gatekeeper ("cannot be opened because the developer
  cannot be verified"): right-click the app, choose **Open**, and confirm once — this is
  only needed the first time.
- **Linux** — download `telemetry-overlay-gui-linux.tar.gz`, extract it, and run
  `./telemetry-overlay-gui` inside the extracted folder. Needs the system OpenGL/xkbcommon
  libraries Qt depends on (`libgl1`, `libegl1`, `libxkbcommon0`, `libxcb-cursor0` on
  Debian/Ubuntu — install with `apt-get install` if the app fails to start).

The downloaded package is self-contained: it bundles Python, PySide6, FFmpeg (via PyAV),
OpenCV and every other dependency. The `cache/` folder it creates on first run lives
next to the executable (see [Cache](#cache)) — if that folder is read-only (e.g. the app
sits in `Program Files`), the app tells you at startup and asks you to move it somewhere
writable, such as your Desktop or Documents folder. The `telemetry-overlay` CLI is not
included in these packages; use the source install below for that.

## Setup

Building from source (for the CLI, or for development) needs Python **3.11 or newer**.
From the project root:

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows
.venv/bin/pip install -r requirements.txt                     # Linux / macOS
```

That pulls in everything, including `matplotlib` (used by the diagnostic plots of
`autosync` and `manualsync`) and `pytest`. No external FFmpeg installation is needed:
PyAV bundles a complete FFmpeg build, including the NVENC hardware encoders — `probe`
reports which of them this machine can actually use.

## Installation

There is no `telemetry-overlay` file in the project root: it is a console script that
only exists once the package is installed into the virtualenv. Pick one of the two forms
below — every example in this README writes `telemetry-overlay`, and you substitute
whichever you chose.

**Installed (recommended).** Install the package once, in editable mode, and the command
becomes available inside the virtualenv:

```bash
.venv/Scripts/python.exe -m pip install -e .      # Windows
.venv/bin/pip install -e .                        # Linux / macOS
```



## GUI

A PySide6 control panel over the CLI commands above, for anyone who would rather drag
files in and click a button than type flags. It is a thin layer: every action calls the
same `cmd_probe`/`cmd_autosync`/`export_video`/... code the CLI uses, so its output and
behaviour match the CLI exactly.

```powershell
.venv\Scripts\telemetry-overlay-gui.exe                                    # empty, drag files in
.venv\Scripts\telemetry-overlay-gui.exe "data\ThumbPW_0024.MP4" "data\2026-08-23 10-23-27.bin"
```

```bash
# Linux / macOS, without installing
PYTHONPATH=src .venv/bin/python -m telemetry_overlay.gui.app data/flight.MP4 data/flight.bin
```

The window is organised as three numbered tabs -- **1 · Align**, **2 · Preview**,
**3 · Export** -- in the order the work actually happens, above a terminal pane shared
by every command the GUI runs. Above the tabs sits a project header that stays visible
whichever tab you are on: the video, log and preset pickers (click one to change it;
drag&drop works anywhere in the window), a **Probe** button that runs `probe` and
prints its report to the terminal, a **Clear cache** button, and -- on the second row
-- the current alignment and the source video's properties.

The alignment shown in that header has three states, and they are the difference
between guessing and knowing what the export will render:

- `! not aligned yet — run Align` — no alignment established for this video;
- `● delay ... · drift ... — not saved` — a working alignment that is **not** in
  `sync.json` yet;
- `✓ delay ... · drift ...` — matches what is on disk.

### 1 · Align

One tab for one job, in two columns: everything you set down the left, everything you
look at down the right. It carries both ways of aligning, because they produce the same
two numbers:

- **Automatic search** — **From**/**To**, **Windows**, **Window length** and an
  optional advanced search-range limit, then **Run auto align**. This runs the very
  same `autosync` code the CLI runs, printing the identical per-window table, verdict
  and progress bar to the terminal.
- **Current alignment** — the **Log delay** and **Clock drift** fields. Clock drift is
  `scale` under its own name: alongside the raw factor the GUI shows what it means
  (`= +1.47 s every 1000 s`), because `1.001467` is impossible to judge on its own.
  `--time-scale` and the `scale` key in `sync.json` are unchanged, so existing files
  keep working.

The result is not a static picture: it lands in an interactive **roll rate** plot of
the log's roll rate (blue, fixed) against the video's (orange), on a shared log-time
axis. **Left-drag the orange trace** to correct the alignment by hand -- it updates the
Log delay field live, exactly as if you had typed it. Scroll to zoom the time axis;
**right-drag up/down rescales the roll-rate axis** (a view change only) for when one
trace's peaks dwarf the other's; **Reset view** re-fits to the video trace's current
position. A "?" next to the plot heading explains all three gestures. Under it, the
**Window fit** pane shows `autosync_fit.png` when the run used more than one window:
one point per window, and a straight line through them is the drift (scroll to zoom,
drag to pan, double-click to re-fit).

Running the search and dragging the plot both change the alignment **for this session
only** -- the preview and the export follow along immediately, but nothing is written
to disk until you press **Save alignment**, which writes this video's `sync.json`. The
header's `— not saved` marker tells you when there is work you have not kept.
**Save diagnostic PNG** writes a snapshot of the current alignment to the video's
`cache/` directory, reusing the same plotting function the CLI uses.

If the current From/To span's optical flow is already fully cached (see [Cache](#cache))
the tab runs the analysis by itself when you open it, since a cache hit costs nothing;
otherwise the plot shows `No analysis yet -- click 'Run auto align'.`

### 2 · Preview

The overlay composited on the actual video frame, exactly like `frame` but live: a
scrub slider, an exact-time field and a **Quality** selector (Full / 1/2 / 1/4).
Decoding happens on a background thread and is debounced (~60 ms), since a distant seek
can take a moment and must not freeze the window; the quality selector trims the
compositing and on-screen scaling cost, not the decode itself.

The slider carries two extra round handles besides the scrub needle: they set the
**From**/**To** range shared with the Align and Export tabs -- edit it here or in
either tab's fields and everything updates together. Click empty groove to jump the
needle there; drag a round handle to move From or To. It defaults to the whole video.

### 3 · Export

Burns the overlay into a video file: output path (browsable, defaulting to
`<video>.overlay.mp4` next to the source video), **From**/**To**, an **Encoder**
dropdown populated from the encoders actually usable on this machine
(`available_encoders()` -- the same hardware-first probing `probe` uses), a **Quality**
field (blank keeps the encoder's own default), a **Downscale** factor for a fast draft
export, and **Copy audio**/**Overwrite if it exists** checkboxes. It does not go
through `cmd_export` -- see the note on the `--scale`/`--time-scale` collision in
`cli.py` below -- but calls `export_video()` directly with the current preset and
shared sync, so the result is identical to running `export` from the CLI with the same
options. The terminal shows the same `
`-updated progress bar and the same final
summary line (frames, fps, encoder, audio, band coverage, output size) the CLI prints.

Every parameter field has a small "?" next to its label; hover it for a description of
what that option does.

## How to run the command

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
# Linux / macOS equivalent
PYTHONPATH=src .venv/bin/python -m telemetry_overlay probe data/flight.MP4 data/flight.bin
```

`$env:PYTHONPATH` lasts for the current shell session only, so set it once per terminal.
Note the quotes: paths containing spaces — like the sample log — need them.

Some examples below are run against the sample flight in `data/`. Those files are too
large to keep in git and are not part of a fresh clone: substitute your own video and
log, the portable `flight.MP4`/`flight.bin` examples show the shape of each command.

## Commands

```bash
telemetry-overlay probe      flight.MP4 flight.bin    # what the two files contain
telemetry-overlay autosync   flight.MP4 flight.bin    # suggest the sync (never applied by itself)
telemetry-overlay manualsync flight.MP4 flight.bin --log-delay 171.8   # check a sync by eye
telemetry-overlay frame      flight.MP4 flight.bin --at 40 --log-delay 171.8   # iterate on the look
telemetry-overlay export     flight.MP4 flight.bin --from 210 --to 220         # write the video
```

Two global flags go **before** the subcommand name:

- `--version` — print the package version and exit.
- `-v`, `--verbose` — raise the logging level; repeatable (`-vv` for debug).

All five subcommands take the video as the first positional. `probe` takes the log as an
optional second positional; the other four require it.

### Shared options

`-p`, `--preset PATH` is accepted by `frame`, `autosync`, `manualsync` and `export`
(default: `presets/default.json`, see [Presets](#presets)). `autosync` and `manualsync`
accept it only for consistency — neither uses it.

The sync options below are accepted by `frame`, `manualsync` and `export`. `autosync`
does **not** take them: it computes the sync instead of receiving it.

- `--log-delay SECONDS` — the log time matching video time zero (advanced; the direct
  form, handy for nudging a number you already have).
- `--anchor-video-time SECONDS` / `--anchor-log-time SECONDS` — the alternative form,
  given as a pair: the video and the log timestamps of one moment you recognise in both
  (e.g. takeoff). The log delay is computed from the two. Mutually exclusive with
  `--log-delay`, and the two must be given together.
- `--time-scale FACTOR` — clock-drift scale in
  `log_time = log_delay + video_time * scale` (default `1.0`). Combines with either form
  above and needs one of them present. Not to be confused with `export`'s `--scale`,
  which downscales the output frame.

When none of them is given, `frame` and `export` fall back to the video's saved sync
(see [Cache](#cache)), and to `0` / `1.0` if there is no such file. `manualsync` has no fallback: it requires
`--log-delay` or the anchor pair.

`--save-sync` is accepted by `frame` and `export` only. It writes the log delay and
scale in effect — whether given on the command line or read back from an existing file —
to the **video**'s `sync.json` under `cache/` (see [Cache](#cache)), whatever the log is
called. If they came from the anchor pair, both timestamps are stored too, so the file
stays readable and re-editable.

### `probe` — see what you have

Reports the video's exact frame rate, duration and codec, which encoders this machine
can use, and — when a log is given — every telemetry channel found with its source,
rate, valid fraction and range, plus the flight modes, the arming time and the range of
log delays for which the clip fits inside the log.

```
telemetry-overlay probe <video> [log]
```

- `video` — path to the video file (required).
- `log` — path to the `.bin` DataFlash log (optional). Without it, only the video and
  the encoder list are reported.

```bash
telemetry-overlay probe flight.MP4 flight.bin
telemetry-overlay probe flight.MP4               # video and encoders only
```

```powershell
telemetry-overlay probe "data\ThumbPW_0024.MP4" "data\2026-08-23 10-23-27.bin"
```

An encoder marked `no` is simply unavailable here; on a machine without an NVIDIA GPU
only the software encoders show up, which is fine, just slower.

### `frame` — iterate on the look

Renders one composited frame to a PNG in about a second, and prints the log time, flight
mode, flight timer, every channel value and the messages on screen at that instant. This
is the fast loop for adjusting a preset or a log delay, with no export to wait for.

```
telemetry-overlay frame <video> <log> [-p PRESET]
    [--log-delay SECONDS | --anchor-video-time SECONDS --anchor-log-time SECONDS]
    [--time-scale FACTOR] [--save-sync]
    [--at SECONDS] [-o PATH] [--width PIXELS] [--overlay-only]
```

- `video`, `log` — required positionals.
- `--at SECONDS` — video time to render (default `0`).
- `-o`, `--output PATH` — where to write the PNG (default: `out/frame.png`).
- `--width PIXELS` — downscale the saved image to this width; full resolution by
  default. Handy for a quick look without waiting on a 4K PNG.
- `--overlay-only` — save the transparent HUD alone, as an alpha-channel PNG, instead of
  compositing it onto the video frame.
- plus the shared preset and sync options above.

```bash
telemetry-overlay frame flight.MP4 flight.bin --at 42 --log-delay 250 -o out/f.png
telemetry-overlay frame flight.MP4 flight.bin --at 42 --overlay-only   # HUD alone
telemetry-overlay frame flight.MP4 flight.bin --at 42 --width 960 -o out/preview.png
```

The horizon is the most sensitive thing to check the sync against: at a sharp roll, half
a second of error is obvious, while speed and altitude change far too slowly to judge.

### `autosync` — suggest a log delay and clock-drift scale

Measures how fast the image rotates (optical flow) and correlates it with the roll rate
in the log. It runs the optical flow once across the whole span, picks the windows where
the image actually rotates the most (calm cruise or straight legs cannot correlate
against anything, so there is no point spending a window on them), correlates each
against the log, and fits a log delay *and* a scale through the windows that agree with
each other — clocks drift, so a single offset that matches at one point in the video can
be seconds off at another. Prints the per-window table and a confidence verdict, and
changes nothing unless you pass `--write`.

```
telemetry-overlay autosync <video> <log> [-p PRESET]
    [--from SECONDS] [--to SECONDS] [--windows N] [--window-length SECONDS]
    [--search-min SECONDS] [--search-max SECONDS] [--write] [--plot DIR]
```

- `video`, `log` — required positionals.
- `--from SECONDS` — video time to start spreading windows from (default `0`).
- `--to SECONDS` — video time to stop spreading windows at (default: end of video).
- `--windows N` — number of analysis windows, picked from wherever the image rotates the
  most within `--from`/`--to`, spaced at least one window apart (default `6`). Each is
  scored independently, and only the ones that agree with each other — a distinct
  correlation peak, and a consistent log-delay-vs-time line — feed the fit; a window
  that is active but matched the wrong manoeuvre is excluded, not averaged in.
  `--windows 1` disables the scale fit, analyses `--from`→`--to` as one slice, and keeps
  `scale` at `1.0`.
- `--window-length SECONDS` — duration of each window (default `20`). Longer windows
  correlate more reliably but cost more decode time; the sample video needed `40` to get
  a confident peak per window.
- `--search-min` / `--search-max SECONDS` — bound the log delay the estimate may return,
  in **log** seconds — the same quantity `probe` reports as "valid log delays". Applied
  to every window.
- `--write` — store the accepted estimate (log delay **and** scale) in the video's
  `sync.json` (see [Cache](#cache)). Without it, `autosync` only prints the suggestion; nothing is
  ever written automatically.
- `--plot DIR` — save `autosync_diagnostics.png` (roll and roll-rate overlay for the
  *whole* analysed `--from`→`--to` span, from the single continuous optical-flow pass —
  not just the best-scoring window) and, with more than one window, `autosync_fit.png`
  (each window's log delay against its position in the video, trustworthy vs. discarded,
  and the fitted line, whose slope *is* the drift).
- `-p`, `--preset PATH` — accepted, unused.

Needs visible, textured ground and real manoeuvring. Footage of empty sky, straight and
level flight, or a gimballed camera will not correlate in any window; that is what the
per-window and overall confidence verdicts are for. Always verify with `frame` or
`manualsync` at more than one point in the video before trusting the result.

```bash
telemetry-overlay autosync flight.MP4 flight.bin --search-min 170 --search-max 420
telemetry-overlay autosync flight.MP4 flight.bin --search-min 170 --search-max 420 --write
```

Against the sample data, using the log window `probe` reported (`171.7` → `415.9`), with
40 s windows because the default 20 s were too short for a confident peak on this
footage:

```powershell
telemetry-overlay autosync "data\ThumbPW_0024.MP4" "data\2026-08-23 10-23-27.bin" `
    --window-length 40 --search-min 171.7 --search-max 415.9 --plot out
```

That found `log_delay 414.057s`, `scale 1.00137` from 3 windows (correlation up to
`0.98`) spanning 168 s — consistent with the manually-found `414.130` around video time
100 s, and with the drift that made a fixed offset lose sync by video time 200 s.

Not every clip correlates this cleanly. On footage with a less distinctive roll signal
(similar turns repeated throughout, or long calm stretches) the windows may not agree
closely enough to pass the fit's checks, even after several tries with different
`--window-length`/`--windows` values — `autosync` then says `check it by eye` rather than
guess. That is the design, not a bug: fall back to anchoring by eye at more than one
point in the video.

### `manualsync` — check a chosen log delay by eye

Plots one video slice's optical-flow roll rate against the log's roll and roll rate,
shifted by a sync you already picked (by hand, or from `autosync`). Unlike `autosync` it
searches for nothing; it just draws the two-panel diagnostic for that slice so you can
see whether the traces line up.

```
telemetry-overlay manualsync <video> <log> [-p PRESET]
    [--from SECONDS] [--to SECONDS]
    (--log-delay SECONDS | --anchor-video-time SECONDS --anchor-log-time SECONDS)
    [--time-scale FACTOR] [--plot DIR]
```

- `video`, `log` — required positionals.
- `--from` / `--to SECONDS` — the video slice to analyse, in video seconds (default: the
  whole video, `0` to its end).
- `--plot DIR` — directory to save `manualsync_diagnostics.png` in (default: `out`).
- plus the shared preset and sync options above — with the sync **required** here: there
  is no saved-sync fallback, and no `--save-sync`.

```powershell
telemetry-overlay manualsync "data\ThumbPW_0024.MP4" "data\2026-08-23 10-23-27.bin" `
    --from 60 --to 90 --log-delay 206.1
```

### `export` — write the final video

Decodes, composites the HUD and encodes, copying the audio packet by packet. Prints
throughput and ETA while it runs, and the encoder, band coverage and file size at the
end.

```
telemetry-overlay export <video> <log> [-p PRESET]
    [--log-delay SECONDS | --anchor-video-time SECONDS --anchor-log-time SECONDS]
    [--time-scale FACTOR] [--save-sync]
    [-o PATH] [--from SECONDS] [--to SECONDS]
    [--encoder KEY] [--quality N] [--scale FACTOR] [--no-audio] [-y]
```

- `video`, `log` — required positionals.
- `-o`, `--output PATH` — where to write the file (default: `<video stem>.overlay.mp4`,
  next to the source video).
- `--from SECONDS` — trim start, in video seconds (default: from the beginning).
- `--to SECONDS` — trim end, in video seconds (default: to the end of the video).
- `--encoder KEY` — one of `nvenc_h264`, `nvenc_hevc`, `x264`, `x265`; see `probe` for
  which of these this machine actually supports. Default: the best available, NVENC
  first, falling back to x264.
- `--quality N` — CQ (NVENC) or CRF (x264/x265) value; lower is higher quality and a
  larger file. Default depends on the chosen encoder. The output bitrate is also capped
  near the source video's own bitrate (when the source reports one), so raising quality
  past what the source needs stops paying for it in file size once the cap is hit.
- `--scale FACTOR` — downscale the output by this factor (e.g. `0.5` for half
  resolution) for a fast draft export while iterating on a preset or a sync. The HUD is
  unaffected: element geometry is frame-relative, so it renders correctly at any size.
  Default: `1.0`, the source resolution.
- `--no-audio` — drop the audio track instead of copying it untouched.
- `-y`, `--overwrite` — overwrite `--output` if it already exists; otherwise `export`
  refuses to clobber an existing file.
- plus the shared preset and sync options above.

```bash
# 10-second test segment first: seconds instead of minutes, and half resolution for speed
telemetry-overlay export flight.MP4 flight.bin --log-delay 250 --from 100 --to 110 \
    --scale 0.5

# then the whole clip at full resolution, with an explicit encoder and quality
telemetry-overlay export flight.MP4 flight.bin --log-delay 250 \
    --encoder x264 --quality 20 -o flight_hud.mp4 -y
```

Expect roughly 9 fps at 4K with the software encoder — about 12 minutes for a 4-minute
clip — and far less with NVENC. Always render a short segment before committing to the
whole flight.

## Synchronising video and telemetry

The camera clock cannot be trusted — on real footage the MP4 creation time can be off by
more than a year — so the alignment is explicit:

```
log_time = log_delay + video_time * scale
```

The **log delay** is the log time corresponding to video time zero. The **scale**
corrects clock drift between camera and flight controller; it defaults to `1.0` and stays
close to it, but over a few minutes the difference is enough to lose sync by the end even
when the start lines up perfectly.

**Raising the log delay moves the overlay earlier in the video, not later.** A higher log
delay matches video time zero against a log time further into the flight, so every log
event appears sooner on screen. In practice: overlay ahead of the picture (already
climbing while the aircraft is still on the ground) → lower the log delay; overlay
lagging the picture → raise it.

To find the log delay, pick one event you can see in the video *and* find in the log —
takeoff rotation, a sharp roll, touchdown — and pass both timestamps to
`--anchor-video-time`/`--anchor-log-time`; no mental subtraction needed. `probe` prints
the range of log delays for which the clip fits inside the log, as a sanity check. Then
refine by rendering frames with `frame` and nudging `--log-delay` in shrinking steps
(±5 s, ±1 s, ±0.2 s), checking against a sharp roll.

Scale needs two points far apart in the video: a single anchor cannot tell drift from a
plain offset. If a log delay matched by eye at one point is off at another several
minutes away, that difference is the drift. `autosync` fits both quantities at once
across several windows; alternatively set `--time-scale` by hand and verify at the far
point.

Save the result with `--save-sync` (writes the video's `sync.json` under `cache/`,
keeping the anchor pair alongside the log delay if that is what you used). Later commands pick it
up automatically whenever `--log-delay`, `--anchor-*` and `--time-scale` are all omitted.

The sync file lives apart from the preset on purpose: a preset describes a *look* and is
reused across flights, while a log delay belongs to one video/log pair.

## Presets

`presets/default.json` holds the layout, the theme and the units. Positions are
normalised to the frame (0..1) and all sizes are fractions of frame height, so a preset
made against a 1080p proxy renders identically on a 4K master.

Element types: `readout` (any telemetry channel), `heading`, `mode`, `timer`, `horizon`,
`wind`, `messages`. Each entry takes `x`, `y`, `anchor`, `visible`, `scale`, `unit`,
`decimals` and a type-specific `options` block — see the docstrings in
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
| Wind speed/direction | `XKF2.VWN/VWE` — EKF3 wind estimate | none, by design |
| Mode, messages | `MODE`, `MSG` | — |

**AGL shows `NO AGL` most of the time, and that is correct.** A rangefinder is a
short-range sensor — around 12 m on the test aircraft — so it only reads near the
ground, during takeoff, landing and low passes. There is deliberately no fallback to
barometric or terrain height: AGL here means a measured height. A hysteresis filter
(default 0.3 s) keeps the readout from strobing at the edge of the sensor's range.

**The wind arrow is nose-relative, not north-up.** It points where the wind is
blowing *toward*, relative to the aircraft's heading (straight up = dead ahead) — the
same convention as a G1000's wind vector, and the only one readable on a frame that is
always nose-forward rather than compass-oriented. There is no fallback for wind: a log
without an EKF3 wind estimate shows "no data" rather than guessing.

Status messages are guaranteed at least one second on screen each. When several arrive
in the same millisecond they queue rather than flash by. The boot banner and everything
logged before arming is hidden by default.

Parsed logs are cached, so only the first read costs anything. The cache invalidates
itself when the log changes or when the channel definitions in `fields.py` are edited.
See [Cache](#cache) for where it lives.

## Cache

Everything the program computes from a video or a log goes under a `cache/` folder, one
directory per source file (named after the file plus a hash of its full path, so two
clips with the same name never collide). That folder lives next to the running program:
in the repo root when running from source, next to the `.exe`/binary when running a
downloaded package (see [Download](#download-no-python-required)).

| File | What it is |
|---|---|
| `telemetry.npz` | the parsed `.bin`, so only the first read costs anything |
| `roll-rate.npz` | the optical-flow roll rate, the slowest thing here (see below) |
| `plots/` | diagnostic PNGs the GUI regenerates on every run |
| `sync.json` | **not cache** — your log delay, see below |

All of it is derived and safe to delete; it is rebuilt on demand. Set
`TELEMETRY_OVERLAY_CACHE` to put the directory somewhere other than its default location.

**The optical-flow cache fills in as you go.** Its value for a given pair of consecutive
frames does not depend on the time range you asked for — a range only decides *which*
pairs get computed. So rather than caching "the analysis of 90–130s", it keeps one array
covering the whole video plus a record of which entries are real, and every request fills
only the holes inside it. Analysing 100–115s with `manualsync` and then the whole clip
with `autosync` computes 100–115s once, not twice; the same range twice is free; overlapping or
disjoint ranges in any order never recompute a pair. Measured on the sample footage:
re-running the same 15s slice went from 18.8s to 0.02s, and widening a cached 100–115s
slice to 90–130s cost 33% less than computing it from scratch — with a bit-identical
result.

**`sync.json` lives there but is not cache.** It holds the alignment you set by hand,
which nothing can recompute, so the GUI's **Clear cache** button and `clear_cache_for()`
both leave it alone. Alongside the log delay it records two timestamps: `created`, when
the alignment was first established (kept across later saves), and `updated`, rewritten
every time the file is saved — the one to check when asking whether a sync is still the
one you were working on. Files written by older versions, next to the video as
`<video>.sync.json`, are still read if the new location is empty; the next save moves
them.

## Re-encoding notes

Burning pixels into a picture requires decoding, compositing and encoding the video
stream: there is no way to avoid re-encoding it. What this tool does instead is keep the
loss negligible and the process fast:

- constant-quality encoding (NVENC CQ or x264 CRF) at the original resolution and exact
  frame rate, preserving the source's colour range and matrix, with the bitrate also
  capped near the source's own so re-encoding does not inflate the file size;
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
log), unit conversions, preset round-trips, band geometry, the sync cross-correlation
against signals with a known log delay, and the GUI terminal pane's carriage-return
handling (`tests/test_gui.py`).
