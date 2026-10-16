Discord symbol from the official brand assets:
https://discord.com/branding

Source SVG:
https://cdn.prod.website-files.com/6257adef93867e50d84d30e2/66e3d7f4ef6498ac018f2c55_Symbol.svg

The original SVG is included unchanged. discord.png is a 12×9 rasterization
for the native BUSY Bar image element. A native 16×16 rounded rectangle
provides the Discord blurple background (#5865F2).
The Discord mark belongs to Discord Inc.

mic-off.svg and headphones-off.svg are small status glyphs made for the
9×9 overlay in the lower-right corner. A native dark rounded rectangle
separates the white glyph and red strike from the logo.

Regenerate the PNG assets with `sh scripts/render-icons.sh` (requires
`rsvg-convert` from librsvg). Rendering dependencies aren't needed at runtime.

The current overlay has independent `microphone.svg` and `headphones.svg`
glyphs. `status-glyph-*.anim` scales between them, and `status-strike-*.anim`
draws/erases the red strike without changing the underlying glyph. Both
use transparent BGRA frames in the firmware's bicycle0 format (raw encoding).
The format follows busy-app/busybar-firmware `scripts/seq2anim.py`, tag 1.1.1;
the generator here is an independent minimal writer.

`scripts/render-status-animations.py` requires `rsvg-convert` and `ffmpeg`.
It also creates `docs/status-transitions.gif` from the same vector sources.

Normal mode shows only the Discord logo. Entering mute/deafen grows the badge;
returning to normal scales it down to zero while erasing the strike.
Playback is 48 fps: glyph swaps take about 167 ms and strike changes 125 ms.

The dark badge background stays fixed during microphone/headphone swaps.
The background scales with the glyph only on entry from and exit to normal; it does not fade.

The strike draws and erases in the same direction: top-left to bottom-right.
Erasing advances the line start toward the fixed bottom-right endpoint.
