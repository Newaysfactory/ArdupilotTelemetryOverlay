"""Where derived, regenerable files live: one directory per source file.

Everything the program computes from a video or a log -- the parsed telemetry, the
optical-flow roll rate, diagnostic plots -- is expensive to produce and cheap to
throw away. Keeping it beside the source files scattered them through the user's
media folders; keeping it here means a single place to inspect, and a single place
to wipe when something goes wrong (see :func:`clear_cache_for`).

One directory per *source file*, not per project: a log paired with two different
videos must not re-parse, and a video re-analysed against a different log must not
re-run its optical flow. The directory name carries the readable file name plus a
hash of its absolute path, so two files with the same name in different folders
cannot collide.

The sync file is the deliberate exception (see :data:`PRESERVED_NAMES`): it lives
here so the user's media folders stay clean, but it is *user data* -- the result of
aligning the video by hand -- not something that can be recomputed. It survives
:func:`clear_cache_for`.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

from .paths import executable_dir

#: Override for tests and for anyone who wants the cache off its default volume.
_ENV_VAR = "TELEMETRY_OVERLAY_CACHE"

#: Files inside a cache directory that :func:`clear_cache_for` must never delete,
#: because they hold work the user did by hand and cannot be recomputed.
PRESERVED_NAMES = frozenset({"sync.json"})


def cache_root() -> Path:
    """The ``cache/`` directory: overridable via ``TELEMETRY_OVERLAY_CACHE``,
    otherwise next to the running executable (the repo root in dev mode, next
    to the ``.exe``/binary in a packaged build -- see :mod:`telemetry_overlay.paths`)."""
    override = os.environ.get(_ENV_VAR)
    return Path(override) if override else executable_dir() / "cache"


def cache_dir_for(source: str | Path) -> Path:
    """Cache directory belonging to one video or log file. Not created here."""
    source = Path(source).resolve()
    digest = hashlib.sha256(str(source).encode("utf-8")).hexdigest()[:8]
    # "montagna.MP4" -> "montagna-MP4-1a2b3c4d": readable at a glance, unique per
    # absolute path, and free of characters that need quoting on any filesystem.
    suffix = source.suffix.lstrip(".")
    stem = _sanitise(source.stem)
    name = f"{stem}-{suffix}-{digest}" if suffix else f"{stem}-{digest}"
    return cache_root() / name


def ensure_cache_dir_for(source: str | Path) -> Path:
    path = cache_dir_for(source)
    path.mkdir(parents=True, exist_ok=True)
    return path


def clear_cache_for(*sources: str | Path) -> list[Path]:
    """Delete the regenerable files cached for ``sources``; keep user data.

    Returns what was removed, for reporting. Anything named in
    :data:`PRESERVED_NAMES` stays, and a directory left holding only preserved
    files is kept rather than removed.
    """
    removed: list[Path] = []
    for source in sources:
        if source is None:
            continue
        directory = cache_dir_for(source)
        if not directory.is_dir():
            continue
        for entry in sorted(directory.iterdir()):
            if entry.name in PRESERVED_NAMES:
                continue
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
            removed.append(entry)
        # Only tidy the directory away if nothing preserved is left in it.
        if not any(directory.iterdir()):
            directory.rmdir()
    return removed


def _sanitise(name: str) -> str:
    """Keep the name recognisable while dropping anything awkward in a path."""
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name) or "unnamed"
