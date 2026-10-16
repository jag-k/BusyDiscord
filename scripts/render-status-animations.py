"""Build transparent 9x9 BUSY Bar animations from editable SVG glyphs.

Requires rsvg-convert and ffmpeg. Uses uncompressed BGRA bicycle0 frames,
compatible with the published firmware's scripts/seq2anim.py (1.1.1).
"""

import base64
import shutil
import struct
import subprocess
import tempfile

from pathlib import Path


ASSETS = Path(__file__).resolve().parents[1] / "src" / "busydiscord" / "assets"
SIZE = 9
FPS = 48
HEADER = "<8sBBBBBHBIIIII"
RSVG = shutil.which("rsvg-convert") or "/usr/bin/rsvg-convert"
FFMPEG = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"


def raster(svg):
    png = subprocess.run(  # noqa: S603 -- Local renderer, fixed arguments and repository-owned SVG sources.
        [RSVG], input=svg.encode(), capture_output=True, check=True
    ).stdout
    return subprocess.run(  # noqa: S603 -- Local ffmpeg, fixed arguments; SVG raster bytes on stdin.
        [FFMPEG, "-v", "error", "-i", "pipe:0", "-f", "rawvideo", "-pix_fmt", "bgra", "pipe:1"],
        input=png,
        capture_output=True,
        check=True,
    ).stdout


def glyph(name, scale=1, background=1):
    source = (ASSETS / f"{name}.svg").read_text()
    body = source[source.index(">") + 1 : source.rindex("</svg>")]
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="9" height="9">'
        f'<g transform="translate(4.5 4.5) scale({background}) translate(-4.5 -4.5)">'
        '<rect width="9" height="9" rx="2" fill="#23272A"/></g>'
        f'<g transform="translate(4.5 4.5) scale({scale}) translate(-4.5 -4.5)">'
        f"{body}</g>"
        f"</svg>"
    )


def strike(progress, *, erasing=False):
    if erasing:
        start = 0.5 + (1 - progress) * 8
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" width="9" height="9">'
            f'<path d="M{start} {start}L8.5 8.5" fill="none" stroke="#FF3333" '
            'stroke-width="1.5" stroke-linecap="round"/></svg>'
        )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="9" height="9">'
        f'<path d="m.5.5 8 8" fill="none" stroke="#FF3333" stroke-width="1.5" stroke-linecap="round"'
        f' stroke-dasharray="{progress * 11.314} 20"/>'
        f"</svg>"
    )


def encode(filename, sections):
    # Changing the source restarts the firmware's player after a one-shot ends.
    # Give each section its own file rather than relying on section-only updates.
    if len(sections) > 1:
        for name, sequence in sections.items():
            encode(filename.replace(".anim", f"-{name}.anim"), {name: sequence})
        return
    frames = []
    ranges = []
    for name, sequence in sections.items():
        start = len(frames)
        frames.extend(raster(svg) for svg in sequence)
        ranges.append((name, start, len(frames) - 1))
    ranges.insert(0, ("default", 0, len(frames) - 1))
    section_size = sum(14 + len(name) for name, _, _ in ranges)
    frame_size = 4 + SIZE * SIZE * 4
    offset = struct.calcsize(HEADER) + section_size
    entries = b"".join(
        struct.pack("<IIIB", start, end, offset + start * frame_size, 1) + name.encode() + b"\0"
        for name, start, end in ranges
    )
    body = b"".join(struct.pack("<BBH", 0, 1, len(frame)) + frame for frame in frames)
    header = struct.pack(
        HEADER,
        b"bicycle0",
        0,
        SIZE,
        SIZE,
        2,
        FPS,
        SIZE * SIZE * 4,
        0,
        section_size,
        len(body),
        len(ranges),
        len(frames),
        len(frames),
    )
    (ASSETS / filename).write_bytes(header + entries + body)


def main():
    shrinking = (1, 0.75, 0.4, 0.1)
    growing = (0.1, 0.4, 0.75, 1)
    encode(
        "status-glyph.anim",
        {
            "show_mic": [glyph("microphone", scale, background=scale) for scale in (0, 0.2, 0.4, 0.65, 0.85, 1)],
            "show_headphones": [glyph("headphones", scale, background=scale) for scale in (0, 0.2, 0.4, 0.65, 0.85, 1)],
            "hide_mic": [glyph("microphone", scale, background=scale) for scale in (1, 0.85, 0.65, 0.4, 0.2, 0)],
            "hide_headphones": [glyph("headphones", scale, background=scale) for scale in (1, 0.85, 0.65, 0.4, 0.2, 0)],
            "mic": [glyph("microphone")],
            "headphones": [glyph("headphones")],
            "to_mic": [glyph("headphones", scale) for scale in shrinking]
            + [glyph("microphone", scale) for scale in growing],
            "to_headphones": [glyph("microphone", scale) for scale in shrinking]
            + [glyph("headphones", scale) for scale in growing],
        },
    )
    empty = '<svg xmlns="http://www.w3.org/2000/svg" width="9" height="9"/>'
    progress = (0.05, 0.2, 0.4, 0.65, 0.85, 1)
    encode(
        "status-strike.anim",
        {
            "clear": [empty],
            "hold": [strike(1)],
            "show": [strike(value) for value in progress],
            "hide": [strike(value, erasing=True) for value in reversed(progress[:-1])] + [empty],
        },
    )
    # A shareable, enlarged preview rendered from the same source glyphs.
    frames = [("microphone", 0, 0, 0)] * 36
    entering = (0, 0.2, 0.4, 0.65, 0.85, 1)
    frames.extend(("microphone", scale, slash, scale) for scale, slash in zip(entering, progress, strict=True))
    frames.extend([("microphone", 1, 1, 1)] * 36)
    frames.extend(("microphone", scale, 1, 1) for scale in shrinking)
    frames.extend(("headphones", scale, 1, 1) for scale in growing)
    frames.extend([("headphones", 1, 1, 1)] * 36)
    hiding = [-value for value in reversed(progress[:-1])] + [0]
    frames.extend(("headphones", scale, slash, scale) for scale, slash in zip(reversed(entering), hiding, strict=True))
    frames.extend([("headphones", 0, 0, 0)] * 36)
    raw = bytearray()
    # Use the packaged logo PNG in previews to preserve its exact 12x9 geometry.
    logo = base64.b64encode((ASSETS / "discord.png").read_bytes()).decode()
    for icon, scale, slash, background in frames:
        icon_svg = glyph(icon, scale, background=background)
        icon_body = icon_svg[icon_svg.index(">") + 1 : icon_svg.rindex("</svg>")]
        slash_svg = strike(abs(slash), erasing=slash < 0) if slash else "<svg></svg>"
        slash_body = slash_svg[slash_svg.index(">") + 1 : slash_svg.rindex("</svg>")]
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16">'
            f'<rect width="16" height="16" rx="3" fill="#5865F2"/>'
            f'<image href="data:image/png;base64,{logo}" x="2" y="3" width="12" height="9"/>'
            f'<g transform="translate(7 7)">{icon_body}{slash_body}</g>'
            f"</svg>"
        )
        raw.extend(raster(svg))
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "status-transitions.gif"
        subprocess.run(  # noqa: S603 -- Local ffmpeg and a generated temporary output path.
            [
                FFMPEG,
                "-v",
                "error",
                "-f",
                "rawvideo",
                "-pixel_format",
                "bgra",
                "-video_size",
                "16x16",
                "-framerate",
                str(FPS),
                "-i",
                "pipe:0",
                "-filter_complex",
                "scale=192:192:flags=neighbor,split[a][b];[a]palettegen=max_colors=32[p];[b][p]paletteuse=dither=none",
                "-loop",
                "0",
                "-y",
                str(output),
            ],
            input=raw,
            check=True,
        )
        target = ASSETS.parents[2] / "docs" / "status-transitions.gif"
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(output.read_bytes())


if __name__ == "__main__":
    main()
