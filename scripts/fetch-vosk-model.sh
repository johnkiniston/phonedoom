#!/bin/sh
# Download the small English Vosk model used for voice commands (Apache-2.0, about 40 MB).
set -e
DEST="${1:-models}"
NAME=vosk-model-small-en-us-0.15
SHA256=30f26242c4eb449f948e42cb302dd7a686cb29a3423a8367f99ff41780942498
mkdir -p "$DEST"
tmp="$(mktemp)"
curl -fsSL -o "$tmp" "https://alphacephei.com/vosk/models/$NAME.zip"
sum="$(sha256sum "$tmp" 2>/dev/null || shasum -a 256 "$tmp")"
[ "${sum%% *}" = "$SHA256" ] || { echo "checksum mismatch, not installing" >&2; rm -f "$tmp"; exit 1; }
unzip -q -o "$tmp" -d "$DEST"
rm -f "$tmp"
echo "installed $DEST/$NAME"
