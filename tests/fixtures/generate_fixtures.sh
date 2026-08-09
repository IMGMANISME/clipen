#!/usr/bin/env bash
# Generates short test clips with real audio (macOS `say`) + a solid-color
# video track (ffmpeg lavfi), so ASR/shot/silence detection all have
# something real to chew on. Not committed to git (see .gitignore) --
# re-run this after a fresh checkout before running the test suite.
set -euo pipefail

FIXTURES_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

make_clip() {
  local name="$1" text="$2" color="$3"
  local aiff="$FIXTURES_DIR/${name}.aiff"
  local out="$FIXTURES_DIR/${name}.mp4"

  say -o "$aiff" "$text"
  duration="$(ffprobe -v quiet -show_entries format=duration -of csv=p=0 "$aiff")"
  ffmpeg -y -f lavfi -i "color=c=${color}:s=640x360:d=${duration}" -i "$aiff" \
    -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest "$out"
  rm -f "$aiff"
}

# "intro" and "intro-take2" share a take-suffix filename convention on
# purpose -- they exercise the local take-collapsing heuristic in
# editing/narrative.py. "walk" is a distinct, unrelated clip.
make_clip "intro" "Hi everyone, welcome back to my channel. Today we are exploring the city." "blue"
make_clip "intro-take2" "Hey everyone, welcome back to my channel. Today we're exploring downtown." "navy"
make_clip "walk" "Let's start walking around and see what we can find in this neighborhood." "green"

echo "Fixtures generated in $FIXTURES_DIR"
