#!/usr/bin/env bash
# MultiCapture の macOS 配布物（arm64）を作る。
#
# 使い方:
#   ./build_mac.sh [--ffmpeg PATH] [--jobs N]
#     --ffmpeg PATH  FFmpeg を自前でビルドせず、このバイナリを同梱する（動的リンクのものは配布に使わないこと）
#     --jobs N       FFmpeg ビルドの並列数（既定: CPU 数）
#   環境変数 PYTHON  使う python3（tkinter つき。既定: python3）
#   署名: MC_SIGN_KEYCHAIN / MC_SIGN_KEYCHAIN_PASS / MC_SIGN_HASH が揃っていればその自己署名証明書、なければ ad-hoc。
#
# 手順: ①FFmpeg をソースからビルド（LGPL・静的。.build/ にキャッシュ）②native/mac/build.sh で mc-capture をビルド
#       ③PyInstaller で .app を作り、補助プログラムと FFmpeg を入れる ④内側から順に署名 ⑤ditto で ZIP にする
# 出力: dist/MultiCapture-<version>-macos-arm64.zip
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD="$ROOT/.build"
DIST="$ROOT/dist"
NAME="MultiCapture"
BUNDLE_ID="io.github.satoa-may5.multicapture"
MIN_OS="15.0"

# FFmpeg のソース（公式リリース）。更新するときは URL とハッシュを一緒に直す。
FFMPEG_VER="9.0.2"
FFMPEG_URL="https://ffmpeg.org/releases/ffmpeg-$FFMPEG_VER.tar.xz"
FFMPEG_SHA256="8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e"

FFMPEG_BIN=""
JOBS="$(sysctl -n hw.ncpu)"
while [ $# -gt 0 ]; do
  case "$1" in
    --ffmpeg) FFMPEG_BIN="$2"; shift 2;;
    --jobs) JOBS="$2"; shift 2;;
    *) echo "不明な引数: $1" >&2; exit 2;;
  esac
done

die() { echo "エラー: $*" >&2; exit 1; }

# ---- 前提の確認 ----
[ "$(uname -s)" = Darwin ] || die "macOS で実行してください。"
[ "$(uname -m)" = arm64 ] || die "Apple Silicon（arm64）の Mac が必要です。"
xcode-select -p >/dev/null 2>&1 && command -v swiftc >/dev/null && command -v clang >/dev/null \
  || die "Xcode Command Line Tools が必要です（xcode-select --install）。"
for c in curl tar make ditto codesign shasum; do
  command -v "$c" >/dev/null || die "$c が見つかりません。"
done
PYTHON="${PYTHON:-python3}"
command -v "$PYTHON" >/dev/null || die "python3 が見つかりません（brew install python@3.14 python-tk@3.14）。"
"$PYTHON" -c 'import tkinter' 2>/dev/null \
  || die "この Python は tkinter を使えません（brew install python-tk@3.14）: $PYTHON"
if [ -n "$FFMPEG_BIN" ]; then [ -x "$FFMPEG_BIN" ] || die "--ffmpeg に指定したファイルが実行できません: $FFMPEG_BIN"; fi
if [ -n "${MC_SIGN_KEYCHAIN:-}" ] || [ -n "${MC_SIGN_KEYCHAIN_PASS:-}" ] || [ -n "${MC_SIGN_HASH:-}" ]; then
  : "${MC_SIGN_KEYCHAIN:?MC_SIGN_KEYCHAIN が未設定}" "${MC_SIGN_KEYCHAIN_PASS:?MC_SIGN_KEYCHAIN_PASS が未設定}" "${MC_SIGN_HASH:?MC_SIGN_HASH が未設定}"
  SELF_SIGN=1
else
  SELF_SIGN=0
fi

VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/multicapture/__init__.py")"
[ -n "$VERSION" ] || die "multicapture/__init__.py からバージョンを読めません。"
echo "MultiCapture $VERSION (macOS arm64)"
mkdir -p "$BUILD" "$DIST"

# ---- 1. FFmpeg（LGPL・静的・外部ライブラリなし） ----
if [ -z "$FFMPEG_BIN" ]; then
  FF_DIR="$BUILD/ffmpeg-$FFMPEG_VER-arm64"
  FFMPEG_BIN="$FF_DIR/ffmpeg"
  if [ ! -x "$FFMPEG_BIN" ]; then
    TAR="$BUILD/ffmpeg-$FFMPEG_VER.tar.xz"
    [ -f "$TAR" ] || curl -fL --retry 3 -o "$TAR" "$FFMPEG_URL"
    echo "$FFMPEG_SHA256  $TAR" | shasum -a 256 -c - || die "FFmpeg ソースのハッシュが一致しません: $TAR"
    SRC="$BUILD/ffmpeg-src-$FFMPEG_VER"
    rm -rf "$SRC" "$FF_DIR"; mkdir -p "$SRC" "$FF_DIR"
    tar -xf "$TAR" -C "$SRC" --strip-components=1
    ( cd "$SRC"
      ./configure --prefix="$FF_DIR/prefix" \
        --disable-autodetect --enable-videotoolbox --enable-audiotoolbox \
        --disable-ffplay --disable-doc --disable-debug \
        --enable-static --disable-shared \
        --arch=arm64 --cc=clang \
        --extra-cflags="-mmacosx-version-min=$MIN_OS" --extra-ldflags="-mmacosx-version-min=$MIN_OS"
      make -j"$JOBS" ffmpeg ffprobe
      cp ffmpeg ffprobe "$FF_DIR/"
      cp COPYING.LGPLv2.1 "$FF_DIR/"
      ./ffmpeg -hide_banner -version | grep -i configuration > "$FF_DIR/configuration.txt" )
    rm -rf "$SRC"
  fi
  FFMPEG_LICENSE="$FF_DIR/COPYING.LGPLv2.1"
  FF_NOTE_VER="$FFMPEG_VER"
else
  FFMPEG_LICENSE=""
  FF_NOTE_VER="$("$FFMPEG_BIN" -version | head -1)"
fi

# FFmpeg の検査：システムのライブラリだけに依存し、必要なエンコーダとフィルタを持つこと
if otool -L "$FFMPEG_BIN" | tail -n +2 | awk '{print $1}' | grep -Ev '^(/System/Library/|/usr/lib/)'; then
  die "FFmpeg がシステム外のライブラリに依存しています（上の行）。静的ビルドを使ってください。"
fi
ENC="$("$FFMPEG_BIN" -hide_banner -encoders)"
for e in h264_videotoolbox aac pcm_s16le; do grep -qw "$e" <<<"$ENC" || die "FFmpeg に $e エンコーダがありません。"; done
FLT="$("$FFMPEG_BIN" -hide_banner -filters)"
for f in scale pad format atrim asetpts afade apad testsrc2 color; do grep -qw "$f" <<<"$FLT" || die "FFmpeg に $f フィルタがありません。"; done
"$FFMPEG_BIN" -hide_banner -version | grep -q -- '--enable-gpl' && die "GPL ビルドの FFmpeg は同梱できません。"

# ---- 2. 補助プログラム mc-capture ----
"$ROOT/native/mac/build.sh"
HELPER="$ROOT/native/mac/build/mc-capture"
[ -x "$HELPER" ] || die "mc-capture のビルドに失敗しました。"

# ---- 3. PyInstaller ----
VENV="$BUILD/venv-mac"
[ -x "$VENV/bin/python" ] || "$PYTHON" -m venv "$VENV"
"$VENV/bin/python" -m pip install --quiet --upgrade pip pyinstaller
"$VENV/bin/python" -c 'import tkinter' || die "venv で tkinter を使えません。"

PKG="$DIST/pkg"
APP="$PKG/$NAME.app"
rm -rf "$PKG" "$BUILD/work-mac"
mkdir -p "$PKG"
"$VENV/bin/python" -m PyInstaller --noconfirm --clean --windowed --onedir \
  --name "$NAME" --osx-bundle-identifier "$BUNDLE_ID" --target-architecture arm64 \
  --add-binary "$FFMPEG_BIN:." \
  --paths "$ROOT" \
  --distpath "$PKG" --workpath "$BUILD/work-mac" --specpath "$BUILD" \
  "$ROOT/MultiCapture.pyw"

PL="$APP/Contents/Info.plist"
pb() { /usr/libexec/PlistBuddy -c "$1" "$PL"; }
pbset() { pb "Set :$1 $2" 2>/dev/null || pb "Add :$1 ${3:-string} $2"; }
pbset CFBundleName "$NAME"
pbset CFBundleDisplayName "$NAME"
pbset CFBundleShortVersionString "$VERSION"
pbset CFBundleVersion "$VERSION"
pbset LSMinimumSystemVersion "$MIN_OS"
pbset NSAudioCaptureUsageDescription "ブラウザの音声を録音するために使います。"
pbset NSHighResolutionCapable true bool

# 補助プログラムは、自分の Info.plist を持つ小さなバンドルとして Contents/Helpers に置く。
# 単体の実行ファイルのままだと、ScreenCaptureKit の開始時に replayd との接続が切られる（-3805）。
HELPER_APP="$APP/Contents/Helpers/$NAME Capture.app"
mkdir -p "$HELPER_APP/Contents/MacOS"
cp "$HELPER" "$HELPER_APP/Contents/MacOS/mc-capture"
HPL="$HELPER_APP/Contents/Info.plist"
plutil -create xml1 "$HPL"
plutil -insert CFBundleIdentifier -string "$BUNDLE_ID.capture" "$HPL"
plutil -insert CFBundleName -string "$NAME Capture" "$HPL"
plutil -insert CFBundleExecutable -string mc-capture "$HPL"
plutil -insert CFBundlePackageType -string APPL "$HPL"
plutil -insert CFBundleShortVersionString -string "$VERSION" "$HPL"
plutil -insert CFBundleVersion -string "$VERSION" "$HPL"
plutil -insert LSMinimumSystemVersion -string "$MIN_OS" "$HPL"
plutil -insert LSUIElement -bool true "$HPL"
plutil -insert NSAudioCaptureUsageDescription -string "ブラウザの音声を録音するために使います。" "$HPL"

# 補助プログラムと FFmpeg が、アプリが探す場所に入っているか
[ -x "$HELPER_APP/Contents/MacOS/mc-capture" ] || die "補助プログラムが $HELPER_APP にありません（helper.py の探索先と合わせてください）。"
[ -x "$APP/Contents/Frameworks/ffmpeg" ] || die "ffmpeg が Contents/Frameworks にありません（ffmpeg_candidates の探索先と合わせてください）。"

# ---- 4. 署名（内側から順に。全部同じ ID） ----
if [ "$SELF_SIGN" = 1 ]; then
  security unlock-keychain -p "$MC_SIGN_KEYCHAIN_PASS" "$MC_SIGN_KEYCHAIN"
  CS=(codesign -f --keychain "$MC_SIGN_KEYCHAIN" -s "$MC_SIGN_HASH" --timestamp=none)
  APP_REQ="designated => identifier \"$BUNDLE_ID\" and certificate leaf = H\"$MC_SIGN_HASH\""
else
  CS=(codesign -f -s - --timestamp=none)
  APP_REQ=""
fi
# Mach-O を、深いパスから順に（シンボリックリンクは対象外）
MACHO="$(mktemp)"
find "$APP" -type f -print0 | while IFS= read -r -d '' f; do
  if file -b "$f" | grep -q 'Mach-O'; then printf '%s\n' "$f"; fi
done | awk '{ n=gsub("/","/"); print n "\t" $0 }' | sort -rn | cut -f2- > "$MACHO"
echo "署名する Mach-O: $(wc -l < "$MACHO") 個"
MAIN_EXE="$APP/Contents/MacOS/$NAME"
while IFS= read -r f; do
  [ "$f" = "$MAIN_EXE" ] && continue    # メイン実行ファイルはアプリの署名と一緒に最後にやる
  case "$f" in
    */Helpers/*.app/Contents/MacOS/mc-capture) ;;   # バンドルごと下で署名する
    */Frameworks/ffmpeg)     "${CS[@]}" -i "$BUNDLE_ID.ffmpeg" "$f";;
    *) "${CS[@]}" "$f";;
  esac
done < "$MACHO"
rm -f "$MACHO"
"${CS[@]}" -i "$BUNDLE_ID.capture" "$HELPER_APP"
find "$APP" -type d -name '*.framework' -print 2>/dev/null \
  | awk '{ n=gsub("/","/"); print n "\t" $0 }' | sort -rn | cut -f2- | while IFS= read -r d; do "${CS[@]}" "$d"; done
if [ -n "$APP_REQ" ]; then
  "${CS[@]}" -i "$BUNDLE_ID" -r="$APP_REQ" "$APP"
else
  "${CS[@]}" -i "$BUNDLE_ID" "$APP"
fi
codesign --verify --deep --strict --verbose=2 "$APP"
codesign -d -r- "$APP" 2>&1 | grep -i designated || true

# ---- 5. ZIP ----
PKGNAME="$NAME-$VERSION-macos-arm64"
STAGE="$DIST/$PKGNAME"
rm -rf "$STAGE"; mkdir -p "$STAGE"
ditto "$APP" "$STAGE/$NAME.app"
cp "$ROOT/README.md" "$STAGE/"
{
  echo "This application bundles FFmpeg ($FF_NOTE_VER), used under the GNU LGPL v2.1 or later."
  echo "FFmpeg is built without any GPL or non-free components, as a static executable."
  echo "Source: $FFMPEG_URL"
  echo "SHA-256: $FFMPEG_SHA256"
  echo "Configuration: $("$FFMPEG_BIN" -hide_banner -version | grep -i '^configuration:' | sed -e 's/^configuration: *//' -e 's/--prefix=[^ ]* *//')"
  echo "FFmpeg is a trademark of Fabrice Bellard. https://ffmpeg.org/legal.html"
} > "$STAGE/FFMPEG_NOTICE.txt"
[ -z "$FFMPEG_LICENSE" ] || cp "$FFMPEG_LICENSE" "$STAGE/FFMPEG_COPYING.LGPLv2.1.txt"
ZIP="$DIST/$PKGNAME.zip"
rm -f "$ZIP"
( cd "$DIST" && ditto -c -k --keepParent "$PKGNAME" "$ZIP" )
rm -rf "$STAGE" "$PKG"
echo "Built: $ZIP ($(du -h "$ZIP" | cut -f1))"
