#!/bin/sh
# Records docs/assets/demo.gif, from the root of the repository.
#
#   sh docs/demo/record.sh
#
# VHS runs demo.tape in the image of docs/demo/Dockerfile, which installs this checkout.
# Then the last screen, the failed run and its exit code, is put first as well, so a GIF
# shown paused (reduced motion, a preview) shows the result and not an empty terminal.
set -eu
docker build -f docs/demo/Dockerfile -t highbar-demo .
docker run --rm -v "$PWD:/vhs" highbar-demo docs/demo/demo.tape
docker run --rm -v "$PWD:/vhs" --entrypoint sh highbar-demo -c '
  set -eu
  cd docs/assets
  ffmpeg -hide_banner -loglevel error -y -sseof -0.1 -i demo.gif -update 1 -frames:v 1 /tmp/last.png
  ffmpeg -hide_banner -loglevel error -y -loop 1 -t 1.5 -framerate 25 -i /tmp/last.png -i demo.gif \
    -filter_complex "[0:v]format=rgb24,fps=25[a];[1:v]format=rgb24,fps=25[b];[a][b]concat=n=2:v=1:a=0,split[x][y];[x]palettegen=max_colors=64:stats_mode=full[p];[y][p]paletteuse=dither=none:diff_mode=rectangle" \
    -loop 0 /tmp/demo.gif
  cp /tmp/demo.gif demo.gif
  # The GIF opens on its last screen, or the paused GIF shows something else (review of #105).
  frames=$(ffmpeg -hide_banner -loglevel error -i demo.gif -f framemd5 - | grep -v "^#" | awk "{print \$6}")
  if [ "$(echo "$frames" | head -n 1)" != "$(echo "$frames" | tail -n 1)" ]; then
    echo "demo.gif does not open on its last screen" >&2
    exit 1
  fi
'
