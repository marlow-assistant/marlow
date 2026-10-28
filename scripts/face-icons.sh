#!/bin/bash
# Cuts the iOS home-screen icons from the full-size face portraits.
#
#   scripts/face-icons.sh <originals-dir> [face-id ...]
#
# <originals-dir> holds marlow-1.png .. marlow-11.png, the 1254px portraits.
# They are kept out of git (only the resized assets the app ships are
# committed); marlow-1.png is also marlow-icon-square.png at the repo root.
# With no ids, every face is cut. Needs ffmpeg.
#
# The icon is the least-cropped of a face's three framings, and by default not
# cropped at all (Mike, 2026-10-16). The other two, the header and large
# avatars, are cropped live in the apps (MarlowFace.swift, web lib/faces.ts).
# A crop here is the same model: the portrait scaled by ZOOM about the anchor
# (X, Y as fractions), so the face crop tuner's icon values paste straight in.
set -euo pipefail

src=${1:?usage: scripts/face-icons.sh <originals-dir> [face-id ...]}
shift
ids=("$@")
[ ${#ids[@]} -gt 0 ] || ids=(1 2 3 4 5 6 7 8 9 10 11)

assets=$(cd "$(dirname "$0")/.." && pwd)/ios/Marlow/Marlow/Assets.xcassets

# id: zoom x y
crop() {
  case $1 in
    1) echo "1.08 0.70 0.00" ;;
    2) echo "1.00 0.50 0.50" ;;
    3) echo "1.10 0.69 0.00" ;;
    4) echo "1.02 0.50 0.50" ;;
    5) echo "1.00 0.50 0.00" ;;
    6) echo "1.03 0.50 0.00" ;;
    7) echo "1.00 0.50 0.36" ;;
    8) echo "1.02 0.50 0.00" ;;
    9) echo "1.06 0.58 0.13" ;;
    10) echo "1.11 0.50 0.00" ;;
    11) echo "1.18 0.50 0.00" ;;
    *) echo "1 0.5 0.5" ;;
  esac
}

for id in "${ids[@]}"; do
  read -r zoom x y < <(crop "$id")
  # Face 1 is the primary icon; the rest are alternates named in project.yml.
  if [ "$id" = 1 ]; then
    out=$assets/AppIcon.appiconset/AppIcon-1024.png
  else
    out=$assets/AppIcon$id.appiconset/AppIcon$id-1024.png
  fi
  [ -f "$out" ] || { echo "no icon set for face $id: $out" >&2; exit 1; }
  ffmpeg -loglevel error -y -i "$src/marlow-$id.png" \
    -vf "crop=iw/$zoom:ih/$zoom:(iw-iw/$zoom)*$x:(ih-ih/$zoom)*$y,scale=1024:1024:flags=lanczos" \
    -frames:v 1 "$out"
  echo "face $id: zoom $zoom, anchor $x, $y -> ${out#$assets/}"
done
