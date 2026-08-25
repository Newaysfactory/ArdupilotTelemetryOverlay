"""Command line interface: ``telemetry-overlay <command>``."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import __version__
from .hud.preset import Preset
from .sync import SyncModel
from .telemetry import read_log
from .telemetry.model import TelemetryLog
from .units import format_duration

DEFAULT_PRESET = Path(__file__).resolve().parents[2] / "presets" / "default.json"


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="telemetry-overlay",
        description="Burn ArduPilot telemetry onto flight video as an HUD/OSD overlay.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "-v", "--verbose", action="count", default=0, help="repeat for more logging"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    probe = subparsers.add_parser(
        "probe", help="report what the video and the log contain"
    )
    probe.add_argument("video", type=Path)
    probe.add_argument("log", type=Path, nargs="?")
    probe.set_defaults(func=cmd_probe)

    frame = subparsers.add_parser(
        "frame", help="render one composited frame to a PNG (fast iteration on the look)"
    )
    _add_common(frame)
    frame.add_argument(
        "--at", type=float, default=0.0, help="video time in seconds (default 0)"
    )
    frame.add_argument("-o", "--output", type=Path, default=Path("out/frame.png"))
    frame.add_argument(
        "--width",
        type=int,
        help="downscale the saved PNG to this width (full resolution by default)",
    )
    frame.add_argument(
        "--overlay-only",
        action="store_true",
        help="save the transparent overlay instead of the composite",
    )
    frame.set_defaults(func=cmd_frame)

    autosync = subparsers.add_parser(
        "autosync",
        help="suggest a log delay and clock-drift scale by correlating image and log "
        "roll rate",
    )
    _add_common(autosync, with_log_delay=False)
    autosync.add_argument(
        "--from",
        dest="start",
        type=float,
        default=0.0,
        help="video time to start spreading windows from",
    )
    autosync.add_argument(
        "--to",
        dest="end",
        type=float,
        help="video time to stop spreading windows at (default: end of video)",
    )
    autosync.add_argument(
        "--windows",
        type=int,
        default=6,
        help="number of analysis windows spread across --from/--to; 1 disables the "
        "scale fit and keeps scale at 1.0 (old behaviour)",
    )
    autosync.add_argument(
        "--window-length",
        type=float,
        default=20.0,
        help="duration of each analysis window, in seconds",
    )
    autosync.add_argument(
        "--search-min", type=float, help="earliest log time to consider"
    )
    autosync.add_argument("--search-max", type=float, help="latest log time to consider")
    autosync.add_argument(
        "--write",
        action="store_true",
        help="write the estimate (log delay and scale) to the video's .sync.json "
        "(never automatic)",
    )
    autosync.add_argument(
        "--plot",
        type=Path,
        help="save diagnostic plots (per-window roll rate and, with --windows > 1, "
        "the delay-vs-time fit) to this directory",
    )
    autosync.set_defaults(func=cmd_autosync)

    manualsync = subparsers.add_parser(
        "manualsync",
        help="plot a chosen log delay against video/log roll, to check it by eye",
    )
    _add_common(manualsync, with_log_delay=False)
    manualsync.add_argument(
        "--from",
        dest="start",
        type=float,
        default=0.0,
        help="video start time, s (default: 0)",
    )
    manualsync.add_argument(
        "--to",
        dest="end",
        type=float,
        default=None,
        help="video end time, s (default: end of video)",
    )
    _add_sync_args(manualsync)
    manualsync.add_argument(
        "--plot",
        type=Path,
        default=Path("out"),
        help="directory to save manualsync_diagnostics.png in (default: out)",
    )
    manualsync.set_defaults(func=cmd_manualsync)

    export = subparsers.add_parser("export", help="write the video with the overlay")
    _add_common(export)
    export.add_argument(
        "-o", "--output", type=Path, help="default: out/<video stem>.overlay.mp4"
    )
    export.add_argument(
        "--from", dest="start", type=float, help="trim start, in video seconds"
    )
    export.add_argument(
        "--to", dest="end", type=float, help="trim end, in video seconds"
    )
    export.add_argument(
        "--encoder", help="encoder key; see 'probe' for what this machine supports"
    )
    export.add_argument("--quality", type=int, help="CQ/CRF value; lower is better")
    export.add_argument(
        "--scale",
        type=float,
        help="downscale factor for a fast draft export, e.g. 0.5 for half resolution",
    )
    export.add_argument(
        "--no-audio", action="store_true", help="drop the audio track instead of copying"
    )
    export.add_argument("-y", "--overwrite", action="store_true")
    export.set_defaults(func=cmd_export)

    return parser


def _add_common(parser: argparse.ArgumentParser, *, with_log_delay: bool = True) -> None:
    parser.add_argument("video", type=Path)
    parser.add_argument("log", type=Path)
    parser.add_argument(
        "-p",
        "--preset",
        type=Path,
        default=DEFAULT_PRESET,
        help=f"overlay preset (default: {DEFAULT_PRESET.name})",
    )
    if with_log_delay:
        _add_sync_args(parser)
        parser.add_argument(
            "--save-sync",
            action="store_true",
            help="store the log delay in the video's .sync.json",
        )


def _add_sync_args(parser: argparse.ArgumentParser) -> None:
    """``--log-delay`` and the ``--anchor-*`` pair: two ways to say the same thing.

    Give ``--log-delay`` directly if you already know it (e.g. from ``autosync``'s
    suggestion, or a previous run). Otherwise give ``--anchor-video-time`` and
    ``--anchor-log-time``: the video and log timestamps of one moment you recognise in
    both (e.g. takeoff) — the log delay is computed from that pair instead of you doing
    the subtraction by hand.
    """
    parser.add_argument(
        "--log-delay",
        dest="log_delay",
        type=float,
        help="log time matching video time zero, in seconds (advanced; prefer "
        "--anchor-video-time/--anchor-log-time)",
    )
    parser.add_argument(
        "--anchor-video-time",
        dest="anchor_video_time",
        type=float,
        help="video time, in seconds, of a moment you can also find in the log "
        "(e.g. takeoff); pair with --anchor-log-time",
    )
    parser.add_argument(
        "--anchor-log-time",
        dest="anchor_log_time",
        type=float,
        help="log time, in seconds, of the same moment given in --anchor-video-time",
    )
    parser.add_argument(
        "--time-scale",
        dest="scale",
        type=float,
        help="clock-drift scale, log_time = log_delay + video_time * scale (advanced; "
        "default 1.0, or from autosync's estimate); combines with either --log-delay "
        "or the --anchor-* pair. Not to be confused with export's --scale, which "
        "downscales the video frame",
    )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_probe(args: argparse.Namespace) -> int:
    from .video.encoders import ENCODERS, is_available
    from .video.probe import probe_video

    info = probe_video(args.video)
    print(f"video: {info.path.name}")
    print(f"  resolution   {info.resolution}  rotation {info.rotation}°")
    print(
        f"  frame rate   {info.frame_rate} "
        f"({float(info.frame_rate):.3f} fps, exact fraction)"
    )
    print(
        f"  frames       {info.frame_count}  duration {info.duration:.3f}s "
        f"({format_duration(info.duration)})"
    )
    print(f"  codec        {info.codec} / {info.pix_fmt}")
    if info.has_audio:
        print(f"  audio        {info.audio_codec} @ {info.audio_rate} Hz (copyable)")
    else:
        print("  audio        none")
    if info.creation_time:
        print(
            f"  created      {info.creation_time.isoformat()} "
            "(camera clocks are often wrong; not used for sync)"
        )

    print("\nencoders available here:")
    for spec in ENCODERS:
        mark = "yes" if is_available(spec.codec) else "no "
        print(f"  [{mark}] {spec.key:<11} {spec.description}")

    if args.log:
        log = read_log(args.log, progress=lambda msg: print(f"\n{msg}"))
        _print_log_report(log, info.duration)
    return 0


def _print_log_report(log: TelemetryLog, video_duration: float) -> None:
    print(f"\nlog: {Path(log.path).name}")
    print(
        f"  window       {log.t_start:.1f}s -> {log.t_end:.1f}s "
        f"({log.duration:.1f}s of flight controller time)"
    )
    if log.arm_time is not None:
        print(f"  armed at     {log.arm_time:.1f}s")
    print(f"  video length {video_duration:.1f}s")
    usable = log.duration - video_duration
    if usable > 0:
        print(
            f"  the clip fits inside the log: valid log delays run from "
            f"{log.t_start:.1f} to {log.t_start + usable:.1f}"
        )
    else:
        print("  the video is longer than the log: part of it cannot be annotated")

    print("\n  channels:")
    for name, channel in log.channels.items():
        span = channel.valid_range()
        span_text = f"{span[0]:.2f} .. {span[1]:.2f}" if span else "no valid samples"
        print(
            f"    {name:<8} {channel.source:<14} {len(channel):>6} samples "
            f"{channel.rate_hz:6.1f} Hz  valid {channel.valid_fraction * 100:5.1f}%  "
            f"{span_text}"
        )
    if log.missing:
        print(f"    missing: {', '.join(log.missing)}")

    print(f"\n  flight modes ({len(log.modes)}):")
    for mode in log.modes:
        print(f"    {mode.t:8.1f}s  {mode.name}")
    print(f"\n  status messages: {len(log.messages)}")


def cmd_frame(args: argparse.Namespace) -> int:
    import numpy as np
    from PySide6.QtGui import QImage, QPainter

    from .hud.renderer import OverlayRenderer
    from .video.export import _display_size  # geometry helper, shared on purpose
    from .video.probe import probe_video
    from .video.reader import FrameReader

    info = probe_video(args.video)
    log = read_log(args.log, progress=_note)
    preset = Preset.load(args.preset)
    sync = _resolve_sync(args, args.video)
    width, height = _display_size(info)

    renderer = OverlayRenderer(preset, width, height)
    timeline = _timeline(info, log, preset, sync)
    index = info.frame_index_at(args.at)
    state = timeline.state(index)

    overlay = renderer.render_frame(state)
    if args.overlay_only:
        image = overlay
    else:
        with FrameReader(args.video, info) as reader:
            rgb = np.ascontiguousarray(
                reader.frame_at_index(index).to_ndarray(format="rgb24")
            )
        image = QImage(
            rgb.data, info.width, info.height, info.width * 3, QImage.Format.Format_RGB888
        ).convertToFormat(QImage.Format.Format_RGBA8888)
        painter = QPainter(image)
        painter.drawImage(0, 0, overlay)
        painter.end()

    if args.width:
        image = image.scaledToWidth(args.width)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if not image.save(str(args.output)):
        print(f"could not write {args.output}", file=sys.stderr)
        return 1

    print(f"frame {index} at video {args.at:.3f}s -> log {state.log_time:.3f}s")
    print(f"  mode {state.mode or '-'}   flight time {_fmt(state.flight_time)}")
    for name in state.values:
        value = state.value(name)
        print(f"  {name:<8} {'-' if value is None else f'{value:.2f}'}")
    if state.messages:
        print(f"  messages: {' | '.join(state.messages)}")
    print(f"wrote {args.output}")
    return 0


def cmd_autosync(args: argparse.Namespace) -> int:
    from .autosync import SyncFitOptions, estimate_sync
    from .video.probe import probe_video

    info = probe_video(args.video)
    log = read_log(args.log, progress=_note)
    options = SyncFitOptions(
        start=args.start,
        end=args.end,
        n_windows=args.windows,
        window_length=args.window_length,
        search_min=args.search_min,
        search_max=args.search_max,
        collect_diagnostics=bool(args.plot),
    )
    end = args.end if args.end is not None else info.duration
    print(
        f"analysing {options.n_windows} window(s) of {options.window_length:.0f}s "
        f"spread over {args.start:.0f}s -> {end:.0f}s of video "
        "(optical flow vs logged roll rate)..."
    )
    result = estimate_sync(
        args.video,
        log,
        options,
        info=info,
        progress=lambda f: _progress_bar("  tracking", f),
    )
    print()
    if len(result.windows) > 1:
        print("  window start   log delay   correlation  confidence  used")
        for w in result.windows:
            print(
                f"  {w.start:9.1f}s  {w.result.log_delay:10.3f}s  "
                f"{w.result.correlation:11.3f}  {w.result.confidence:9.2f}x  "
                f"{'yes' if w.used else 'no'}"
            )
        print()
    if result.warning:
        print(f"  warning: {result.warning}")
    print(f"  estimated log delay: {result.log_delay:.3f} s")
    print(f"  estimated scale    : {result.scale:.5f}")
    if len(result.windows) > 1:
        print(f"  fit residual       : {result.residual_std * 1000:.0f} ms")
        print(f"  span covered       : {result.span_covered:.0f} s")
    print(
        "  verdict            : "
        + ("looks trustworthy" if result.trustworthy else "check it by eye")
    )
    print(
        f"\n  verify with: telemetry-overlay frame {args.video} {args.log} "
        f"--log-delay {result.log_delay:.3f} --time-scale {result.scale:.5f} "
        f"--at {args.start + 5:.0f}"
    )
    if args.write:
        sync = SyncModel(log_delay=result.log_delay, scale=result.scale, note="autosync")
        path = sync.save(SyncModel.path_for(args.video))
        print(f"  written to {path}")
    if args.plot:
        from .autosync import save_diagnostic_plots, save_fit_plot

        if len(result.windows) > 1:
            path = save_fit_plot(result, args.plot)
            print(f"  fit plot written to {path}")
        if result.diagnostics is None:
            print("  no plot: the estimate failed before the signals were computed")
        else:
            path = save_diagnostic_plots(
                result.diagnostics, result.log_delay, args.plot, scale=result.scale
            )
            print(f"  diagnostics plot written to {path}")
    return 0


def cmd_manualsync(args: argparse.Namespace) -> int:
    from .autosync import compute_manual_diagnostics, save_diagnostic_plots
    from .video.probe import probe_video

    info = probe_video(args.video)
    log = read_log(args.log, progress=_note)
    sync = _sync_from_cli(args)
    if sync is None:
        raise ValueError(
            "give either --log-delay or both --anchor-video-time and --anchor-log-time"
        )
    end = args.end if args.end is not None else info.duration
    print(f"analysing video {args.start:.1f}s -> {end:.1f}s (optical flow)...")
    diagnostics = compute_manual_diagnostics(
        args.video,
        log,
        args.start,
        end,
        info=info,
        progress=lambda f: _progress_bar("  tracking", f),
    )
    print()
    xlim = (sync.log_time(args.start), sync.log_time(end))
    path = save_diagnostic_plots(
        diagnostics,
        sync.log_delay,
        args.plot,
        scale=sync.scale,
        filename="manualsync_diagnostics.png",
        xlim=xlim,
    )
    print(f"  plot written to {path}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from .video.export import ExportOptions, export_video
    from .video.probe import probe_video

    info = probe_video(args.video)
    log = read_log(args.log, progress=_note)
    preset = Preset.load(args.preset)
    sync = _resolve_sync(args, args.video)
    output = args.output or Path("out") / (args.video.stem + ".overlay.mp4")

    options = ExportOptions(
        encoder=args.encoder,
        quality=args.quality,
        scale=args.scale,
        start=args.start,
        end=args.end,
        copy_audio=not args.no_audio,
        overwrite=args.overwrite,
    )
    print(f"{args.video.name} + {args.log.name} -> {output}")
    print(f"  {_describe_sync(sync)}   preset {Path(args.preset).name}")

    result = export_video(
        args.video,
        log,
        preset,
        sync,
        output,
        options,
        info=info,
        progress=lambda p: _progress_bar(
            f"  {p.frames_done}/{p.frames_total}",
            p.fraction,
            suffix=f"{p.fps:5.1f} fps  eta {format_duration(p.eta)}",
        ),
    )
    print()
    print(
        f"done: {result.frames} frames in {result.elapsed:.1f}s "
        f"({result.fps:.1f} fps) with {result.encoder}"
    )
    print(
        f"  audio {'copied without re-encoding' if result.audio_copied else 'not written'}"
        f"   overlay bands covered {result.band_coverage * 100:.1f}% of each frame"
    )
    print(f"  wrote {result.path} ({result.path.stat().st_size / 1e6:.1f} MB)")
    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _timeline(info, log, preset, sync):
    from .telemetry.events import MessageTrack
    from .telemetry.timeline import build_timeline

    video_times = info.frame_times()
    return build_timeline(
        log,
        video_times,
        sync.log_delay + video_times * sync.scale,
        options=preset.sampling,
        message_track=MessageTrack.from_log(log, preset.messages),
    )


def _sync_from_cli(args: argparse.Namespace) -> SyncModel | None:
    """Build a sync from ``--log-delay`` or the ``--anchor-*`` pair, or ``None`` if
    neither was given (the caller then falls back to the companion ``.sync.json``, or
    raises if there is no such fallback)."""
    anchor_video_time = getattr(args, "anchor_video_time", None)
    anchor_log_time = getattr(args, "anchor_log_time", None)
    log_delay = getattr(args, "log_delay", None)
    scale = getattr(args, "scale", None)
    if (anchor_video_time is None) != (anchor_log_time is None):
        raise ValueError(
            "--anchor-video-time and --anchor-log-time must be given together"
        )
    if anchor_video_time is not None:
        if log_delay is not None:
            raise ValueError(
                "use either --log-delay or --anchor-video-time/--anchor-log-time, not both"
            )
        sync = SyncModel.from_anchors(
            anchor_video_time,
            anchor_log_time,
            scale=scale if scale is not None else 1.0,
            note="command line anchors",
        )
        _note(f"{_describe_sync(sync)} (from the anchors given on the command line)")
        return sync
    if log_delay is not None:
        return SyncModel(
            log_delay=log_delay, scale=scale if scale is not None else 1.0,
            note="command line",
        )
    if scale is not None:
        raise ValueError(
            "--time-scale needs either --log-delay or --anchor-video-time/"
            "--anchor-log-time alongside it"
        )
    return None


def _resolve_sync(args: argparse.Namespace, video: Path) -> SyncModel:
    """Command line sync wins, then the companion sync file, then zero."""
    sync = _sync_from_cli(args)
    if sync is None:
        existing = SyncModel.load_for(video)
        if existing is not None:
            sync = existing
            _note(f"{_describe_sync(sync)} (from {existing.source})")
        else:
            sync = SyncModel()
            _note("no log delay given and no .sync.json found: using 0")
    if getattr(args, "save_sync", False):
        path = sync.save(SyncModel.path_for(video))
        _note(f"saved log delay to {path}")
    return sync


def _describe_sync(sync: SyncModel) -> str:
    scale = f"  scale {sync.scale:.5f}" if sync.scale != 1.0 else ""
    if sync.anchor_video_time is not None:
        return (
            f"log delay {sync.log_delay:+.3f}s{scale}  (anchor: video "
            f"{sync.anchor_video_time:.3f}s = log {sync.anchor_log_time:.3f}s)"
        )
    return f"log delay {sync.log_delay:+.3f}s{scale}"


def _fmt(value: float | None) -> str:
    return "-" if value is None else format_duration(value)


def _note(message: str) -> None:
    print(f"  {message}")


def _progress_bar(prefix: str, fraction: float, suffix: str = "") -> None:
    fraction = min(1.0, max(0.0, fraction))
    filled = int(round(fraction * 28))
    bar = "#" * filled + "-" * (28 - filled)
    sys.stdout.write(f"\r{prefix} [{bar}] {fraction * 100:5.1f}% {suffix}")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    level = {0: logging.WARNING, 1: logging.INFO}.get(args.verbose, logging.DEBUG)
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")
    try:
        return int(args.func(args))
    except (
        FileNotFoundError,
        FileExistsError,
        ValueError,
        RuntimeError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
