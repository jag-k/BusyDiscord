#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
assets=src/busydiscord/assets
rsvg-convert -w 12 -h 9 "$assets/discord.svg" -o "$assets/discord.png"
for icon in mic-off headphones-off; do
  rsvg-convert -w 9 -h 9 "$assets/$icon.svg" -o "$assets/$icon.png"
done
for icon in mic-on-5 headphones-on-5 clock-5; do
  rsvg-convert "$assets/$icon.svg" -o "$assets/$icon.png"
done
# Rear OLED glyphs use binary RGB/alpha so antialiased SVG edges stay crisp.
for icon in back-microphone-on back-microphone-off back-headphones-on back-headphones-off; do
  rsvg-convert "$assets/$icon.svg" |
    ffmpeg -v error -i pipe:0 \
      -vf "format=rgba,lut=r='if(gte(val,128),255,0)':g='if(gte(val,128),255,0)':b='if(gte(val,128),255,0)':a='if(gte(val,128),255,0)'" \
      -frames:v 1 -y "$assets/$icon.png"
done
uv run python scripts/render-status-animations.py
