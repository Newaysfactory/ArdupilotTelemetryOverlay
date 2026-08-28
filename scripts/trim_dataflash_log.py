#!/usr/bin/env python
"""Trim an Ardupilot DataFlash ``.bin`` log by end time, to save disk space.

Truncates the file at a message boundary: every message from the start of
the file up to and including the last one before the cutoff is kept, byte
for byte. Nothing is re-encoded, so every FMT/PARM/UNIT/MULT definition
emitted before the cutoff survives as-is and the trimmed file stays a valid
DataFlash log, readable by pymavlink, Mission Planner, MAVExplorer, etc. --
it is simply a shorter prefix of the original file.

Three ways to say where to cut, mutually exclusive:

``--end`` is in the same timebase ``telemetry-overlay probe`` reports
(seconds since flight controller boot, i.e. ``TimeUS / 1e6``), so a value
can be read straight off its "window ...s -> ...s" line or off
``telemetry_overlay.telemetry.dfreader.read_log(...).t_start``/``t_end``.

``--end-clock``/``--end-clock-utc`` are a real wall-clock time instead, e.g.
what a log viewer shows next to each message. Both are resolved with
pymavlink's own GPS-derived clock (``DFReader.clock``, the same one every
pymavlink-based tool including MAVExplorer uses: GPS week/TOW converted to
Unix time, leap seconds included) -- this needs the log to have acquired a
GPS fix early enough to establish that clock. ``--end-clock`` reads/writes
in *this machine's* local timezone, matching what MAVExplorer shows by
default; ``--end-clock-utc`` is the same idea but in UTC, for when you
already have a UTC time in hand (or the two zones happen to coincide). A
bare ``HH:MM:SS`` reuses the log's own date (in whichever zone applies); a
full ISO datetime (``2026-08-23T11:59:42``, optionally with a
``+HH:MM``/``Z`` offset) is honoured as given, ignoring which of the two
flags was used.

Usage:
    python scripts/trim_dataflash_log.py <log.bin> --end 351.67
    python scripts/trim_dataflash_log.py <log.bin> --end-clock 11:59:42
    python scripts/trim_dataflash_log.py <log.bin> --end-clock-utc 2026-08-23T09:59:42Z
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from pymavlink import mavutil
from pymavlink.DFReader import DFMessage


def resolve_end_clock(text: str, mlog, *, local: bool) -> float:
    """Turn ``--end-clock``/``--end-clock-utc``'s value into a Unix timestamp.

    ``mlog`` must already have its GPS-derived clock established (true as
    soon as it is opened: pymavlink pre-scans for a GPS fix in its
    constructor) so its ``timebase`` gives the log's own date, in whichever
    zone applies, to anchor a bare ``HH:MM:SS`` against. ``local`` selects
    this machine's timezone over UTC -- only for a bare time or an ISO
    datetime with no offset; an explicit offset in ``text`` always wins.
    """
    if mlog.clock is None:
        raise SystemExit(
            "this log never establishes a GPS-derived clock (no fix logged "
            "early enough), so --end-clock/--end-clock-utc have nothing to "
            "anchor to -- use --end instead"
        )
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        if local:
            anchor_date = datetime.fromtimestamp(mlog.clock.timebase).date()
        else:
            anchor_date = datetime.fromtimestamp(mlog.clock.timebase, tz=timezone.utc).date()
        dt = datetime.combine(anchor_date, datetime.fromisoformat(f"2000-01-01T{text}").time())
    if dt.tzinfo is None:
        dt = dt.astimezone() if local else dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def trim(src: Path, dst: Path, is_past_cutoff: Callable[[DFMessage], bool]) -> float:
    """Write the prefix of ``src`` up to the cutoff to ``dst``.

    Returns the log time (seconds since flight controller boot) of the last
    sample actually kept.
    """
    mlog = mavutil.mavlink_connection(str(src), robust_parsing=True)
    keep_offset = 0
    last_time = 0.0
    while True:
        msg = mlog.recv_msg()
        if msg is None:
            break
        if is_past_cutoff(msg):
            break
        time_us = getattr(msg, "TimeUS", None)
        if time_us is not None:
            last_time = time_us * 1e-6
        keep_offset = mlog.offset
    dst.write_bytes(mlog.data_map[:keep_offset])
    return last_time


def _by_log_time(end_time: float) -> Callable[[DFMessage], bool]:
    def is_past_cutoff(msg: DFMessage) -> bool:
        time_us = getattr(msg, "TimeUS", None)
        return time_us is not None and time_us * 1e-6 > end_time

    return is_past_cutoff


def _by_wall_clock(end_epoch: float) -> Callable[[DFMessage], bool]:
    def is_past_cutoff(msg: DFMessage) -> bool:
        return msg._timestamp > end_epoch

    return is_past_cutoff


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("log", type=Path, help="source .bin DataFlash log")
    end_group = parser.add_mutually_exclusive_group(required=True)
    end_group.add_argument(
        "--end",
        type=float,
        help="keep data up to this log time in seconds (TimeUS / 1e6), "
        "same number reported by `telemetry-overlay probe`",
    )
    end_group.add_argument(
        "--end-clock",
        help="keep data up to this wall-clock time in *this machine's* local "
        "timezone (HH:MM:SS, using the log's own local date, or a full ISO "
        "datetime) -- matches what MAVExplorer shows by default",
    )
    end_group.add_argument(
        "--end-clock-utc",
        help="same as --end-clock, but in UTC",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="output path (default: <log>_trimmed.bin next to the source)",
    )
    args = parser.parse_args()

    if args.end is not None:
        is_past_cutoff = _by_log_time(args.end)
    else:
        # Opened twice (once here to resolve the clock, once inside trim()):
        # DFReader keeps its whole file mmap'd, so this is cheap, and it
        # keeps trim() a single straightforward pass with no special-casing.
        probe_log = mavutil.mavlink_connection(str(args.log), robust_parsing=True)
        local = args.end_clock is not None
        end_epoch = resolve_end_clock(args.end_clock or args.end_clock_utc, probe_log, local=local)
        is_past_cutoff = _by_wall_clock(end_epoch)

    dst = args.output or args.log.with_name(f"{args.log.stem}_trimmed.bin")
    last_time = trim(args.log, dst, is_past_cutoff)

    before = args.log.stat().st_size
    after = dst.stat().st_size
    print(
        f"{dst} ({after / 1e6:.1f} MB, was {before / 1e6:.1f} MB) "
        f"-- last kept sample at {last_time:.3f}s"
    )


if __name__ == "__main__":
    main()
