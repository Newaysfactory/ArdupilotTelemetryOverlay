"""Assemble the ``licenses/`` folder that ships next to the executable.

The distributed package is not just this project: it carries FFmpeg (with x264 and
x265), Qt, pymavlink, NumPy, OpenCV and matplotlib inside it. GPL and LGPL both
require their terms to travel with the binary, so the package needs the texts, not
just a line in a README.

Most of it can be collected from the installed wheels, and is -- that way the notices
describe the versions actually bundled instead of a list someone updated by hand once.
But two of them, the two that matter most, ship nothing usable:

* ``av``'s wheel declares BSD-3-Clause and ships only PyAV's own BSD text. The FFmpeg
  shared libraries it bundles are built ``--enable-gpl`` (verified: the libx264 and
  libx265 encoders open successfully), so the binaries in ``av.libs/`` are GPLv2+ and
  need the GPL text that the wheel does not carry.
* ``PySide6``'s wheel ships ``LicenseRef-Qt-Commercial.txt`` -- a pointer to the
  *commercial* licence, not the LGPLv3 the open-source build is used under.

Copying dist-info files alone would therefore produce a notices folder that looks
complete and silently omits the two strongest obligations, which is worse than none.
Those two texts are vendored in ``packaging/licenses/`` and added explicitly.

Called from the PyInstaller spec after COLLECT, so it always reflects the environment
the bundle was actually built from. Can also be run by hand to inspect the result:

    python scripts/collect_licenses.py out/licenses
"""

from __future__ import annotations

import re
import shutil
import sys
from importlib.metadata import Distribution, PackageNotFoundError, distribution
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VENDORED = REPO_ROOT / "packaging" / "licenses"

#: Filenames inside a ``*.dist-info`` that carry licence text.
LICENSE_NAME = re.compile(r"(licen[cs]e|copying|notice|authors)", re.IGNORECASE)

#: Bundled components whose licence obligations are not satisfied by anything in
#: their own wheel. Each maps to a vendored text and the explanation that has to
#: accompany it; see the module docstring for how these two were found.
VENDORED_NOTES = {
    "GPL-2.0.txt": (
        "FFmpeg (bundled inside the 'av' / PyAV wheel), including libx264 and "
        "libx265",
        "GPL-2.0-or-later",
    ),
    "LGPL-3.0.txt": (
        "Qt 6 (bundled inside the PySide6 wheels) and pymavlink",
        "LGPL-3.0-only",
    ),
}


def _runtime_distributions() -> list[Distribution]:
    """Every distribution reachable from this project's runtime dependencies.

    Walked from the project's own metadata rather than listing the environment:
    the environment also holds pytest, PyInstaller and their dependencies, which
    are not in the bundle and would make the notices claim more than is shipped.
    Requirements guarded by an ``extra ==`` marker are skipped for the same reason.
    """
    try:
        root = distribution("telemetry-overlay")
    except PackageNotFoundError:
        print(
            "telemetry-overlay is not installed in this environment; "
            "run `pip install -e .` first",
            file=sys.stderr,
        )
        raise

    seen: dict[str, Distribution] = {}
    queue = [root]
    while queue:
        dist = queue.pop()
        key = dist.metadata["Name"].lower().replace("_", "-")
        if key in seen:
            continue
        seen[key] = dist
        for requirement in dist.requires or []:
            if "extra ==" in requirement:
                continue
            name = re.split(r"[\s\[<>=!;(~]", requirement, maxsplit=1)[0].strip()
            if not name:
                continue
            try:
                queue.append(distribution(name))
            except PackageNotFoundError:
                # An optional dependency of a dependency that pip did not install:
                # it is not in the bundle either, so there is nothing to notice.
                continue

    del seen["telemetry-overlay"]
    return [seen[key] for key in sorted(seen)]


def _license_files(dist: Distribution) -> list[Path]:
    base = dist._path if isinstance(getattr(dist, "_path", None), Path) else None
    if base is None or not base.is_dir():
        return []
    return sorted(
        path
        for path in base.rglob("*")
        if path.is_file() and LICENSE_NAME.search(path.name)
    )


def _declared_license(dist: Distribution) -> str:
    """A short licence name for the summary table.

    Sources are tried best-first rather than in metadata order. The free-text
    ``License`` field is last because older wheels put the whole licence *text* in
    it, and its first line is then a copyright notice or a row of ``=`` signs --
    both observed here (cycler, kiwisolver). The table is a summary; the
    authoritative answer is always the copied text.
    """
    meta = dist.metadata

    expression = (meta.get("License-Expression") or "").strip()
    if expression:
        return expression

    classifiers = [
        c.split("::")[-1].strip()
        for c in meta.get_all("Classifier") or []
        if c.startswith("License ::")
    ]
    if classifiers:
        return "; ".join(classifiers)

    first_line = (meta.get("License") or "").strip().splitlines()
    candidate = first_line[0].strip() if first_line else ""
    if candidate and len(candidate) <= 60 and "copyright" not in candidate.lower():
        if any(char.isalnum() for char in candidate):
            return candidate
    return "see the accompanying text"


def build(dest: Path) -> Path:
    """Write the complete ``licenses/`` tree at *dest* and return it."""
    if dest.exists():
        shutil.rmtree(dest)
    third_party = dest / "third-party"
    third_party.mkdir(parents=True)

    shutil.copyfile(REPO_ROOT / "LICENSE", dest / "LICENSE.txt")

    for filename in VENDORED_NOTES:
        shutil.copyfile(VENDORED / filename, third_party / filename)

    rows: list[str] = []
    for dist in _runtime_distributions():
        name = dist.metadata["Name"]
        files = _license_files(dist)
        if files:
            folder = third_party / name
            folder.mkdir(parents=True, exist_ok=True)
            for path in files:
                shutil.copyfile(path, folder / path.name)
            where = f"`third-party/{name}/`"
        else:
            where = "no text in the wheel"
        rows.append(
            f"| {name} | {dist.version} | {_declared_license(dist)} | {where} |"
        )

    (dest / "THIRD-PARTY-NOTICES.md").write_text(
        _notices(rows), encoding="utf-8", newline="\n"
    )
    return dest


def _notices(rows: list[str]) -> str:
    vendored_rows = "\n".join(
        f"| {subject} | {spdx} | `third-party/{filename}` |"
        for filename, (subject, spdx) in VENDORED_NOTES.items()
    )
    return f"""# Third-party notices

ArduPilot Telemetry Overlay is distributed under the GNU General Public License v3,
whose text is in `LICENSE.txt` next to this file.

This package does not contain only that program. It bundles the libraries below so it
can run without anything installed, and their licences travel with it. This file is
generated at build time from the packages actually bundled, so the versions listed are
the versions inside this package.

## The parts that constrain the whole package

**FFmpeg, built with libx264 and libx265 (GPLv2-or-later).** These are what the app
decodes and encodes video with, and they arrive inside the `av` (PyAV) wheel. PyAV's
own Python code is BSD-licensed, but the FFmpeg shared libraries it ships are built
with `--enable-gpl`, so they are under the GPL. Combining them with this program is
why the package as a whole is distributed under the GPL.

**Qt 6 and pymavlink (LGPLv3).** The LGPL requires that you be able to replace these
libraries with your own build. You can: this is a directory-style package, and the Qt
libraries are ordinary separate files under `_internal/`, which you may substitute.
The LGPLv3 is a set of additional permissions layered on the GPLv3, so it has to be
read together with the GPLv3 text in `LICENSE.txt`.

## Corresponding source

The source for this program is at
<https://github.com/Newaysfactory/ArdupilotTelemetryOverlay>.

The source for the GPL-licensed FFmpeg, x264 and x265 inside this package is the
source of the exact PyAV wheel it was built from -- see the `av` version in the table
below -- available from <https://pypi.org/project/av/> and, upstream, from
<https://ffmpeg.org/download.html>, <https://www.videolan.org/developers/x264.html>
and <https://www.videolan.org/developers/x265.html>. If you would rather receive it
directly, open an issue on the repository above and it will be provided.

## Texts that the wheels do not ship

| Component | Licence | Text |
| --- | --- | --- |
{vendored_rows}

## Bundled packages

| Package | Version | Declared licence | Text |
| --- | --- | --- | --- |
{chr(10).join(rows)}
"""


def main() -> None:
    dest = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO_ROOT / "out" / "licenses"
    build(dest.resolve())
    print(f"wrote licence notices to {dest.resolve()}")


if __name__ == "__main__":
    main()
