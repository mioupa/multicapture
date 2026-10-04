#!/bin/bash
# MCSpike.app をビルドして署名する。
# 環境変数 MC_SPIKE_KEYCHAIN / MC_SPIKE_KEYCHAIN_PASS / MC_SPIKE_SIGN_HASH が揃っていれば
# その証明書で、なければ ad-hoc で署名する。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$HERE/build/MCSpike.app"
BIN="$APP/Contents/MacOS/mc-spike"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"

swiftc -O -swift-version 5 -target arm64-apple-macos15.0 \
  -framework ScreenCaptureKit -framework CoreAudio -framework AudioToolbox \
  -framework CoreMedia -framework CoreVideo -framework CoreImage \
  -framework ImageIO -framework UniformTypeIdentifiers -framework CoreGraphics -framework AppKit \
  -o "$BIN" "$HERE"/helper/*.swift

cp "$HERE/helper/Info.plist" "$APP/Contents/Info.plist"

if [[ -n "${MC_SPIKE_KEYCHAIN:-}" && -n "${MC_SPIKE_KEYCHAIN_PASS:-}" && -n "${MC_SPIKE_SIGN_HASH:-}" ]]; then
  security unlock-keychain -p "$MC_SPIKE_KEYCHAIN_PASS" "$MC_SPIKE_KEYCHAIN"
  # 自己署名は信頼されておらず、そのままだと指定要件が cdhash になって再ビルドで変わる。
  # 識別子＋証明書（SHA-1 ハッシュ）を明示して、再ビルドしても同じ要件にする。
  codesign -f --keychain "$MC_SPIKE_KEYCHAIN" -s "$MC_SPIKE_SIGN_HASH" -i dev.multicapture.spike \
    -r="designated => identifier \"dev.multicapture.spike\" and certificate leaf = H\"$MC_SPIKE_SIGN_HASH\"" "$APP"
else
  codesign -f -s - -i dev.multicapture.spike "$APP"
fi

echo "built: $APP"
codesign -d -r- "$APP" 2>&1 | grep -i designated || true
