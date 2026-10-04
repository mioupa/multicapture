#!/bin/bash
# mc-capture をビルドする。
#   native/mac/build/mc-capture      単体のバイナリ（アプリに同梱する用）
#   native/mac/build/MCCapture.app   ソース実行用のバンドル（署名つき）
# 署名：環境変数 MC_SIGN_KEYCHAIN / MC_SIGN_KEYCHAIN_PASS / MC_SIGN_HASH が揃っていればその証明書、なければ ad-hoc。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="$HERE/build"
APP="$OUT/MCCapture.app"
BIN="$OUT/mc-capture"
ID="dev.multicapture.capture"

rm -rf "$APP" "$BIN"
mkdir -p "$OUT" "$APP/Contents/MacOS"

swiftc -O -swift-version 5 -target arm64-apple-macos15.0 \
  -framework AppKit -framework ScreenCaptureKit -framework CoreAudio -framework AudioToolbox \
  -framework AVFAudio -framework AVFoundation -framework CoreMedia -framework CoreVideo \
  -framework CoreGraphics \
  -o "$BIN" "$HERE"/Sources/*.swift

cp "$BIN" "$APP/Contents/MacOS/mc-capture"
cp "$HERE/Info.plist" "$APP/Contents/Info.plist"

if [[ -n "${MC_SIGN_KEYCHAIN:-}" && -n "${MC_SIGN_KEYCHAIN_PASS:-}" && -n "${MC_SIGN_HASH:-}" ]]; then
  security unlock-keychain -p "$MC_SIGN_KEYCHAIN_PASS" "$MC_SIGN_KEYCHAIN"
  # 自己署名は信頼されておらず、そのままだと指定要件が cdhash になって再ビルドで変わる。
  # 識別子＋証明書（SHA-1 ハッシュ）を明示して、再ビルドしても同じ要件にする。
  codesign -f --keychain "$MC_SIGN_KEYCHAIN" -s "$MC_SIGN_HASH" -i "$ID" \
    -r="designated => identifier \"$ID\" and certificate leaf = H\"$MC_SIGN_HASH\"" "$APP"
else
  codesign -f -s - -i "$ID" "$APP"
fi

echo "built: $BIN"
echo "built: $APP"
codesign -d -r- "$APP" 2>&1 | grep -i designated || true
