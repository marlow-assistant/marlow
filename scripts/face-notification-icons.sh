#!/bin/bash
# Cuts the web client's notification icons, one per face, from the portraits
# it already ships (web/public/faces/marlow-N.jpg).
#
#   scripts/face-notification-icons.sh
#
# The service worker shows the chosen face on each notification, not the
# home-screen icon: on Android every notification wore face 1 whatever was
# picked (Mike, 2026-10-16). At notification size the whole portrait is a dark
# smudge, so each icon is that face's header crop, read from web/src/lib/
# faces.ts so the table stays in one place. Rerun after changing a header
# crop. Needs node (22.6+, for type stripping) and ffmpeg.
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
faces=$root/web/public/faces

node --experimental-strip-types --no-warnings -e '
  import("'"$root"'/web/src/lib/faces.ts").then(({ FACES }) => {
    for (const f of FACES) console.log(f.id, f.header.zoom, f.header.anchor[0], f.header.anchor[1]);
  });
' | while read -r id zoom x y; do
  # JPEG: a photographic portrait is ~70KB as PNG at this size, ~10KB here.
  ffmpeg -loglevel error -y -i "$faces/marlow-$id.jpg" \
    -vf "crop=iw/$zoom:ih/$zoom:(iw-iw/$zoom)*$x:(ih-ih/$zoom)*$y,scale=192:192:flags=lanczos" \
    -q:v 3 -frames:v 1 "$faces/icon-$id.jpg"
  echo "face $id: zoom $zoom, anchor $x, $y -> web/public/faces/icon-$id.jpg"
done
