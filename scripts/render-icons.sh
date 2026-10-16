#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
assets=src/busydiscord/assets
rsvg-convert -w 12 -h 9 "$assets/discord.svg" -o "$assets/discord.png"
for icon in mic-off headphones-off; do
  rsvg-convert -w 9 -h 9 "$assets/$icon.svg" -o "$assets/$icon.png"
done
uv run python scripts/render-status-animations.py
