# S8 手順書：権限と署名なし配布の検証

確かめること：
- ① 許可のダイアログに、アプリの名前（MultiCapture Spike）が出るか
- ② 子プロセスの補助プログラム（`mc-capture`）が、アプリの許可を引き継ぐか
- ③ アップデート（再ビルドして置き換え）のあと、許可が残るか。ad-hoc 署名と自己署名証明書で比べる

シェルは fish。以降は `tools/mac-spike/s8` を作業場所にする。

```fish
cd /Volumes/EDILOCA_1TB/Documents/projects/multicapture/tools/mac-spike/s8
```

補助プログラムは先に `tools/mac-spike/build_helper.sh` で作っておく（`build/MCSpike.app/Contents/MacOS/mc-spike` が使われる）。別の場所を使うなら `--helper PATH` を付ける。

## 0. 自己署名の準備（`--sign self` のときだけ）

環境変数を 3 つ設定する。値はリポジトリに書かない。

```fish
set -x MC_SPIKE_KEYCHAIN /path/to/spike.keychain
set -x MC_SPIKE_KEYCHAIN_PASS (read -s -P "keychain pass: ")
set -x MC_SPIKE_SIGN_HASH 証明書のSHA-1ハッシュ
```

## (a) v1 を ad-hoc 署名でビルドして展開する

```fish
./build_s8.sh --sign adhoc --version 1
cp ../build/s8/dist/MultiCaptureSpike-1-adhoc.zip ~/Downloads/
```

1. ビルド末尾の `designated =>` の行を控える（ad-hoc は `cdhash H"..."`）。
2. Finder で `~/Downloads` の ZIP をダブルクリックして展開する。Archive Utility が隔離属性を引き継ぐ。
3. 展開できたことを確認する。

```fish
xattr -l ~/Downloads/"MultiCapture Spike.app"
```

`com.apple.quarantine` が付いていればよい。付いていなければ、次で付ける。

```fish
xattr -w com.apple.quarantine "0083;"(printf %x (date +%s))";Safari;"(uuidgen) ~/Downloads/"MultiCapture Spike.app"
```

4. `~/Downloads/MultiCapture Spike.app` を `/Applications` に移してもよいが、以降はどこに置いたかを統一する。

## (b) 初回起動（Gatekeeper）

1. アプリをダブルクリックする。署名なしなので、ブロックされるはず（その文言を表の「Gatekeeper」欄に書く）。
2. システム設定 →「プライバシーとセキュリティ」を開き、下のほうの「このまま開く」を押す。パスワードか Touch ID を求められる。
3. もう一度開く。

## (c) 許可のダイアログと、補助プログラムの引き継ぎ（①②）

1. 起動後、画面収録とシステムオーディオ録音の許可ダイアログが出る。**ダイアログに出た名前**（「MultiCapture Spike」か、`python3` や `mc-capture` など別の名前か）を表に書く（①）。スクリーンショットも撮る。
2. ダイアログが出ない、または拒否した場合は、システム設定 →「プライバシーとセキュリティ」→「画面収録とシステムオーディオ録音」で MultiCapture Spike をオンにする。「システムオーディオ録音のみ」という欄が別にあれば、そちらもオンにする。どの欄に何という名前で並んだかを書く。
3. 許可を付けたら、アプリを終了して起動し直す（macOS が再起動を求めることがある）。
4. 最後の結果ダイアログを読む。
   - 「画面収録：許可済み」で「取得フレーム数」が 0 より大きい → 補助プログラムが許可を引き継いでいる（②）。
   - 「システム音声：許可済み」で「音声ピーク」が 0 より大きい（440 Hz の音が録れている）→ 音声も引き継いでいる（②）。
   - 許可済みなのにフレームが 0、またはエラーなら、ログの `error` を見る（(f) 参照）。

## (d) v2 に更新して許可が残るか見る（③）

1. アプリを終了する。
2. v2 をビルドする。

```fish
./build_s8.sh --sign adhoc --version 2
cp ../build/s8/dist/MultiCaptureSpike-2-adhoc.zip ~/Downloads/
```

3. 今あるアプリを削除し、新しい ZIP を同じ場所に展開して置き換える（同じパス、同じバンドル ID）。

```fish
rm -rf ~/Downloads/"MultiCapture Spike.app"
```

4. Finder で ZIP を展開する。Gatekeeper のブロックがまた出るか確認し、出たら「このまま開く」を押す。
5. 起動する。次を表に書く。
   - 許可のダイアログがまた出たか。
   - システム設定の一覧で、MultiCapture Spike のスイッチがオンのままか。
   - 結果ダイアログの許可の状態、フレーム数、音声ピーク。
   - 結果ダイアログのバージョンが 2 になっているか。

## (e) 許可をリセットして自己署名で繰り返す

1. 許可を消す。`tccutil` は自分のユーザーの許可なら sudo なしで動くはず。動かないときは sudo を付けて、その旨を書く。

```fish
tccutil reset ScreenCapture dev.multicapture.s8
tccutil reset AudioCapture dev.multicapture.s8
```

2. システム設定の一覧から MultiCapture Spike が消えたことを確認する。残っていれば「−」で削除する。
3. アプリを削除し、(a)〜(d) を `--sign self` で繰り返す（ZIP 名は `MultiCaptureSpike-N-self.zip`）。ビルド前に (0) の環境変数を設定する。ビルド末尾の指定要件が `identifier "dev.multicapture.s8" and certificate leaf = H"..."` になっていることを確認する。
4. v1 → v2 の置き換えのあと、許可が残るか（③）を見る。これが ad-hoc との違い。

## (f) ログと結果

起動のたびに次の 2 つができる。

```fish
ls -t ~/Library/Logs/MultiCaptureSpike | head
cat ~/Library/Logs/MultiCaptureSpike/(ls -t ~/Library/Logs/MultiCaptureSpike | grep json | head -1)
```

- `s8-<日時>.log`：実行したコマンドと出力
- `s8-<日時>.json`：バージョン、署名（`codesign -dvvv`）、隔離属性、`perm` の結果、フレーム数、音声ピーク

結果の JSON は、各回のあとに残しておく。

## 結果の記入表

| 項目 | ad-hoc | 自己署名 |
|---|---|---|
| 指定要件（ビルド末尾） | | |
| 展開後の隔離属性あり（xattr -l） | | |
| Gatekeeper の文言 | | |
| ① ダイアログに出た名前（画面収録） | | |
| ① ダイアログに出た名前（音声） | | |
| 設定の一覧に出た名前・欄 | | |
| ② v1 のフレーム数（0 より大きいか） | | |
| ② v1 の音声ピーク | | |
| ③ v2 で許可のダイアログがまた出たか | | |
| ③ v2 で設定のスイッチは残っているか | | |
| ③ v2 のフレーム数 / 音声ピーク | | |
| tccutil に sudo は要ったか | | |
| 備考 | | |
