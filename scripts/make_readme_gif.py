"""Turn a clip of finished overlay footage into the animated GIF used in the README.

Kept as a script rather than done once by hand so the banner can be regenerated from a
different flight later; the source clip is not in the repo (it is full-resolution
footage), so pass it in:

    python scripts/make_readme_gif.py <video> docs/images/overlay-demo.gif

Quality note: a naive per-frame quantisation gives a different palette on every frame,
which shows up as colours crawling across the sky between frames. This does what
FFmpeg's palettegen/paletteuse pair does -- one palette built from frames sampled
across the whole excerpt, then every frame mapped onto that same palette -- so the
result is stable and compresses far better, since consecutive frames then differ only
where the picture actually changed.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import av
from PIL import Image

#: Wide enough to read the HUD numbers on a README, narrow enough to stay a sane
#: download. GitHub renders README images at roughly this width anyway.
DEFAULT_WIDTH = 560
#: Fast enough to read as motion; every extra frame is a straight cost in bytes.
DEFAULT_FPS = 10.0
#: Frames sampled to build the shared palette. More than this stops changing it.
PALETTE_SAMPLES = 24
#: 128 rather than 256: on this footage the two were indistinguishable side by side
#: (sky, terrain and a green HUD do not need a wide palette) and 128 is ~20% smaller.
DEFAULT_COLORS = 128


def _decode(video: Path, start: float, end: float, width: int, fps: float) -> list[Image.Image]:
    frames: list[Image.Image] = []
    step = 1.0 / fps
    next_time = start
    with av.open(str(video)) as container:
        stream = container.streams.video[0]
        # Sequential read after a single seek: the safe pattern for threaded
        # decoding (see the FrameReader notes in dev_notes/CLAUDE.md).
        stream.thread_type = "AUTO"
        height = round(width * stream.codec_context.height / stream.codec_context.width)
        height -= height % 2
        container.seek(int(start / stream.time_base), stream=stream)
        for frame in container.decode(stream):
            if frame.time is None or frame.time < start - 0.5:
                continue
            if frame.time > end:
                break
            if frame.time + 1e-6 < next_time:
                continue
            next_time += step
            rgb = frame.reformat(width=width, height=height, format="rgb24")
            frames.append(Image.fromarray(rgb.to_ndarray()))
    return frames


def _shared_palette(frames: list[Image.Image], colors: int) -> Image.Image:
    """One palette for the whole animation, from frames spread across it."""
    picks = [frames[i] for i in range(0, len(frames), max(1, len(frames) // PALETTE_SAMPLES))]
    thumb_w = frames[0].width // 4
    thumb_h = frames[0].height // 4
    strip = Image.new("RGB", (thumb_w * len(picks), thumb_h))
    for n, frame in enumerate(picks):
        strip.paste(frame.resize((thumb_w, thumb_h), Image.LANCZOS), (n * thumb_w, 0))
    return strip.quantize(colors=colors, method=Image.Quantize.MEDIANCUT)


def build(
    video: Path,
    out: Path,
    *,
    start: float,
    end: float,
    width: int,
    fps: float,
    colors: int = DEFAULT_COLORS,
    dither: bool = False,
) -> Path:
    frames = _decode(video, start, end, width, fps)
    if not frames:
        raise SystemExit(f"no frames decoded from {video} between {start}s and {end}s")

    palette = _shared_palette(frames, colors)
    # Dithering is off by default: compared frame by frame on real footage it made no
    # visible difference here -- not even in the clear sky, where banding would show
    # first -- while costing about 20% in file size, because the dither noise is
    # exactly the kind of detail that defeats GIF's between-frame compression.
    mode = Image.Dither.FLOYDSTEINBERG if dither else Image.Dither.NONE
    mapped = [frame.quantize(palette=palette, dither=mode) for frame in frames]

    out.parent.mkdir(parents=True, exist_ok=True)
    mapped[0].save(
        out,
        save_all=True,
        append_images=mapped[1:],
        duration=round(1000 / fps),
        loop=0,
        optimize=True,
    )
    size_mb = out.stat().st_size / 1e6
    print(
        f"{out}: {len(mapped)} frames, {mapped[0].width}x{mapped[0].height}, "
        f"{fps:g} fps, {size_mb:.1f} MB"
    )
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--from", dest="start", type=float, default=0.0)
    parser.add_argument("--to", dest="end", type=float, default=float("inf"))
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    parser.add_argument("--fps", type=float, default=DEFAULT_FPS)
    parser.add_argument("--colors", type=int, default=DEFAULT_COLORS)
    parser.add_argument(
        "--dither", action="store_true", help="trade ~20%% more bytes for dithering"
    )
    args = parser.parse_args()
    build(
        args.video, args.out,
        start=args.start, end=args.end, width=args.width, fps=args.fps,
        colors=args.colors, dither=args.dither,
    )


if __name__ == "__main__":
    main()
