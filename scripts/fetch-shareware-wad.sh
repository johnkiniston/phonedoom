#!/bin/sh
# Downloads the Doom shareware IWAD and verifies it is the genuine v1.9 shareware release.
set -eu
DEST="${1:-native/doom1.wad}"
URL="https://archive.org/download/doom-shareware_1996/DOOM1.WAD"
MD5="f0cefca49926d00903cf57551d901abe"
tmp="$(mktemp)"
curl -fL "$URL" -o "$tmp"
if command -v md5sum >/dev/null 2>&1; then sum="$(md5sum "$tmp" | cut -d' ' -f1)"; else sum="$(md5 -q "$tmp")"; fi
if [ "$sum" != "$MD5" ]; then echo "checksum mismatch ($sum), not installing" >&2; rm -f "$tmp"; exit 1; fi
mkdir -p "$(dirname "$DEST")"
mv "$tmp" "$DEST"
echo "installed $DEST"
