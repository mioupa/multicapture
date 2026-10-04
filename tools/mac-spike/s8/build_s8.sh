#!/usr/bin/env bash
# S8 build: PyInstaller .app + inside-out signing + quarantined ZIP.
# Usage: build_s8.sh --sign adhoc|self --version N [--helper PATH]
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SPIKE="$(cd "$HERE/.." && pwd)"
SIGN="" ; VER="" ; HELPER="$SPIKE/build/MCSpike.app/Contents/MacOS/mc-spike"
while [ $# -gt 0 ]; do
  case "$1" in
    --sign) SIGN="$2"; shift 2;;
    --version) VER="$2"; shift 2;;
    --helper) HELPER="$2"; shift 2;;
    *) echo "unknown arg $1" >&2; exit 2;;
  esac
done
[ "$SIGN" = adhoc ] || [ "$SIGN" = self ] || { echo "--sign adhoc|self required" >&2; exit 2; }
[ -n "$VER" ] || { echo "--version N required" >&2; exit 2; }
[ -f "$HELPER" ] || { echo "helper not found: $HELPER" >&2; exit 1; }
HELPER="$(cd "$(dirname "$HELPER")" && pwd)/$(basename "$HELPER")"

if [ "$SIGN" = self ]; then
  : "${MC_SPIKE_KEYCHAIN:?}" "${MC_SPIKE_KEYCHAIN_PASS:?}" "${MC_SPIKE_SIGN_HASH:?}"
fi

VENV="$SPIKE/.venv"
[ -x "$VENV/bin/python" ] || /opt/homebrew/bin/python3 -m venv "$VENV"
if ! "$VENV/bin/python" -c 'import PyInstaller' 2>/dev/null; then
  "$VENV/bin/python" -m pip install pyinstaller
fi

B="$SPIKE/build/s8"
WORK="$B/work-$SIGN-$VER"; DIST="$B/dist/$SIGN-$VER"; GEN="$B/gen-$SIGN-$VER"; OUT="$B/dist"
rm -rf "$WORK" "$DIST" "$GEN"; mkdir -p "$WORK" "$DIST" "$GEN" "$OUT"
cp "$HERE/s8app.py" "$GEN/s8app.py"
cp "$HELPER" "$GEN/mc-capture"
printf 'VERSION = "%s"\nBUILD_STAMP = "%s"\n' "$VER" "$(date +%s)-$VER" > "$GEN/_s8_version.py"

NAME="MultiCapture Spike"
"$VENV/bin/python" -m PyInstaller --windowed --onedir --noconfirm --name "$NAME" \
  --osx-bundle-identifier dev.multicapture.s8 --target-architecture arm64 \
  --add-binary "$GEN/mc-capture:." --paths "$GEN" --hidden-import _s8_version \
  --workpath "$WORK" --distpath "$DIST" --specpath "$WORK" "$GEN/s8app.py"
APP="$DIST/$NAME.app"
PL="$APP/Contents/Info.plist"

pb() { /usr/libexec/PlistBuddy -c "$1" "$PL"; }
pbset() { pb "Set :$1 $2" 2>/dev/null || pb "Add :$1 ${3:-string} $2"; }
pbset LSMinimumSystemVersion 15.0
pbset NSAudioCaptureUsageDescription "ブラウザの音声を録音するために使います。"
pbset CFBundleShortVersionString "$VER"
pbset CFBundleVersion "$VER"
pbset CFBundleName "$NAME"
pbset CFBundleDisplayName "$NAME"
pbset NSHighResolutionCapable true bool

# ---- signing (inside-out) ----
if [ "$SIGN" = self ]; then
  security unlock-keychain -p "$MC_SPIKE_KEYCHAIN_PASS" "$MC_SPIKE_KEYCHAIN"
  CS=(codesign -f --keychain "$MC_SPIKE_KEYCHAIN" -s "$MC_SPIKE_SIGN_HASH" --timestamp=none)
else
  CS=(codesign -f -s - --timestamp=none)
fi
# Mach-O files (resolve symlinks away: only regular files), deepest paths first
MACHO="$(mktemp)"
find "$APP" -type f -print0 | while IFS= read -r -d '' f; do
  if file -b "$f" | grep -q 'Mach-O'; then printf '%s\n' "$f"; fi
done | awk '{ n=gsub("/","/"); print n "\t" $0 }' | sort -rn | cut -f2- > "$MACHO"
echo "Mach-O files: $(wc -l < "$MACHO")"
while IFS= read -r f; do
  case "$f" in
    */mc-capture) "${CS[@]}" -i dev.multicapture.s8.capture "$f";;
    *) "${CS[@]}" "$f";;
  esac
done < "$MACHO"
rm -f "$MACHO"
# frameworks / nested bundles, deepest first, then the app itself
find "$APP" -type d \( -name '*.framework' -o -name '*.app' \) ! -path "$APP" -print 2>/dev/null \
  | awk '{ n=gsub("/","/"); print n "\t" $0 }' | sort -rn | cut -f2- | while IFS= read -r d; do "${CS[@]}" "$d"; done
"${CS[@]}" -i dev.multicapture.s8 "$APP"

codesign --verify --deep --strict --verbose=2 "$APP"
echo "--- designated requirement"
codesign -d -r- "$APP" 2>&1 | grep -i designated || true
codesign -dvvv "$APP" 2>&1 | grep -E 'Identifier|CDHash|Authority|Signature' || true

# ---- package ----
ZIP="$OUT/MultiCaptureSpike-$VER-$SIGN.zip"
rm -f "$ZIP"
ditto -c -k --keepParent "$APP" "$ZIP"
xattr -w com.apple.quarantine "0083;$(printf %x "$(date +%s)");Safari;$(uuidgen)" "$ZIP"
echo "ZIP: $ZIP"
xattr -l "$ZIP"
