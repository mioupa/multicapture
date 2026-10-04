# mc-capture（macOS 補助プログラム）

ScreenCaptureKit と Core Audio タップでブラウザの映像・音声を取り込む Swift 製の補助プログラム。Python とのやり取りは [PROTOCOL.md](PROTOCOL.md) を参照。

## ビルド

```
./build.sh
```

出力は `build/` の下。

- `build/mc-capture`：単体のバイナリ（アプリへ同梱する用）
- `build/MCCapture.app`：ソースから動かす用のバンドル（`dev.multicapture.capture`）

署名は、環境変数 `MC_SIGN_KEYCHAIN`、`MC_SIGN_KEYCHAIN_PASS`、`MC_SIGN_HASH` が揃っていればその証明書で（指定要件は識別子と証明書のハッシュに固定するので、再ビルドしても許可が外れない）、なければ ad-hoc で行う。ad-hoc だと再ビルドのたびに許可がリセットされる。

## 使い方

```
build/MCCapture.app/Contents/MacOS/mc-capture --disclaim serve
```

- ソースから動かすときは `--disclaim` を付ける。TCC（画面収録・オーディオ）は、起動元のターミナルや Python ではなく、このバンドル自身の許可で判定される。自分を責任プロセスにして起動し直す（`responsibility_spawnattrs_setdisclaim`）。
- .app に同梱したときは `--disclaim` を付けない。アプリ本体の許可を引き継ぐ。

## 許可

初回は `request_permission` 命令（または システム設定 > プライバシーとセキュリティ）で、`MultiCapture Capture` に「画面収録」と「オーディオ録音」を許可する。許可したあと、画面収録は補助プログラムの再起動が必要になることがある。
