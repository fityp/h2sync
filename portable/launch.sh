#!/bin/sh
# Starts H2Sync from its portable folder on macOS and Linux.
#
# This computer's Python runtime is unpacked from runtime/ into the temp folder
# (once per runtime version) and the app runs from there, so nothing keeps the
# card busy: it can be ejected while H2Sync is open, and cards mounted without
# execute permission (usual on Linux) still work. Settings stay on the card.
set -e
HERE=$(cd "$(dirname "$0")/.." && pwd)
case "$(uname -s)-$(uname -m)" in
  Darwin-arm64)              T=macos-arm64 ;;
  Darwin-x86_64)             T=macos-x86_64 ;;
  Linux-x86_64)              T=linux-x86_64 ;;
  Linux-aarch64|Linux-arm64) T=linux-aarch64 ;;
  *) echo "H2Sync: no runtime for $(uname -sm)" >&2; exit 1 ;;
esac
V=$(cat "$HERE/runtime/VERSION")
BASE="${TMPDIR:-/tmp}"
BASE="${BASE%/}/h2sync-$(id -u)"
RT="$BASE/runtime-$V-$T"
if [ ! -x "$RT/python/bin/python3" ]; then
  echo "H2Sync: unpacking the $T runtime"
  rm -rf "$RT.part"
  mkdir -p "$RT.part"
  tar -xzf "$HERE/runtime/python-$T.tar.gz" -C "$RT.part"
  rm -rf "$RT"
  mv "$RT.part" "$RT"
fi
rm -rf "$BASE/app"
mkdir -p "$BASE/app"
cp "$HERE"/app/*.py "$BASE/app/"
cd "$BASE"
exec "$RT/python/bin/python3" -E -s -B "$BASE/app/h2sync.py" ui --home "$HERE" "$@"
