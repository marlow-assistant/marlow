#!/bin/bash
# Cuts the web client's home-screen icons, one pair per face, from the
# portraits it already ships (web/public/faces/marlow-N.jpg).
#
#   scripts/face-app-icons.sh
#
# The box serves the manifest with the chosen face's icons
# (server/web_static.py), and Chrome on Android picks up a changed icon on a
# later launch; it also paints the launch splash from that icon. With one
# static manifest, the Android user's phone opened on face 1's splash and kept face 1
# on the home screen whatever was picked (2026-10-19).
#
# Same recipe as the original icons (web/public/icon-512.png and
# icon-maskable-512.png, which stay face 1's): the whole portrait, and for
# maskable the portrait at 408px inside the 512 safe zone on black. No
# face-1 pair here: the default manifest is left as it was. Rerun after
# adding a face. Needs ffmpeg.
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
faces=$root/web/public/faces

for src in "$faces"/marlow-*.jpg; do
  id=${src##*/marlow-}
  id=${id%.jpg}
  [ "$id" = 1 ] && continue
  ffmpeg -loglevel error -y -i "$src" \
    -vf "scale=512:512:flags=lanczos" \
    -frames:v 1 "$faces/app-$id-512.png"
  ffmpeg -loglevel error -y -i "$src" \
    -vf "scale=408:408:flags=lanczos,pad=512:512:52:52:color=black" \
    -frames:v 1 "$faces/app-$id-maskable-512.png"
  echo "face $id -> web/public/faces/app-$id-512.png, app-$id-maskable-512.png"
done
