# MultiCapture macOS対応 要件定義書

- 作成日：2026-10-05
- 対象：MultiCapture 1.2.1（commit e111bb8）
- 読み手：実装を担当するエージェントと開発者

## 0. 進め方のルール

1. 作業は「§10 フェーズ」の順に進める。**Phase 0（検証）が終わったら結果をユーザーに報告する。Phase 1 以降は、ユーザーの Go 判断をもらってから始める。**
2. Windows 版の動作は変えない。例外は §7 に挙げた変更だけとする。Windows の実機確認はユーザーが行う。Windows 側に手を入れたフェーズが終わったら、確認手順（§10 のチェックリスト）を添えて報告する。
3. Python のコードは標準ライブラリだけで書く方針を守る。ネイティブのコードは、Swift で書く補助プログラム（§5.2）の中に収める。ビルドにだけ使うツール（PyInstaller、Xcode Command Line Tools）は使ってよい。
4. 本書に出てくるファイル名と行番号は、1.2.1 時点の調査結果である。編集する前に必ず現物を読むこと。
5. 未確定事項（§12）にぶつかったら、推測で決めずに報告する。

## 1. 背景と目的

MultiCapture は、Web 動画を複数のブラウザで区間に分けて同時に録画し、1 本にまとめるツールである。現在は Windows 11 専用で、映像を Windows Graphics Capture で、ブラウザごとの音声を WASAPI のプロセスループバックで取得している。

今回の目的は 2 つある。

- 同じ機能を macOS（Apple Silicon）でも使えるようにする。
- あわせて、OS に依存する部分を切り出して共通化する。

## 2. 決定事項（ユーザー確認済み）

| 項目 | 決定 |
|---|---|
| 配布 | 他人にも配る。Apple Developer Program には登録しない（Developer ID 署名と公証はしない） |
| 対応環境 | macOS 15 以降、Apple Silicon のみ。Intel Mac には対応しない |
| mac で使える機能 | 「動画を高速録画」と「ページを同時録画」。予約録画（`--start --duration`）は mac では対象外 |
| Windows 側 | OS に依存する部分を共通のインターフェースで切り出し、Windows 版もそれに合わせる。同時録画数の上限は自動計測で決め、NVIDIA の新しい上限（12）に対応する |
| 実装方式（提案どおり） | 映像と音声のキャプチャは Swift の補助プログラムで行う。Python 側は標準ライブラリだけで書く |
| 同時録画数（提案どおり） | 上限は短時間の計測で自動的に決める（Windows と mac で共通） |
| 画面（提案どおり） | mac では、録画中はディスプレイのスリープを止める（必須） |

## 3. スコープ

**対象**

- mac
  - 動画を高速録画
  - ページを同時録画
  - ログイン情報の引き継ぎ
  - 録画中のミュート
  - 配布用の .app（ZIP）
  - README の mac 節
- Windows
  - 共通化のためのリファクタ
  - 同時録画数の自動決定
  - NVENC の上限 12 への対応

**対象外**

- mac での予約録画。mac で `--start` や `--duration` を指定された場合は、「macでは未対応です」と表示し、オプションを無視して通常どおり起動する。
- Intel Mac と macOS 14 以前
- Developer ID 署名、公証、App Store での配布
- Safari と Firefox
- UI の多言語化（日本語のみのまま）

## 4. 現状の Windows 依存（調査結果）

| モジュール | Windows 依存の内容 | mac での扱い |
|---|---|---|
| `win32.py` | import した時点で user32、kernel32、ole32、dwmapi を `WinDLL` で読み込む（5-8）。そのため mac では import の段階で失敗する。ほかにウィンドウの列挙と位置操作、DPI 設定、`KeepAwake`（PowerCreateRequest、263-294）を持つ | Windows 専用の実装に移す。mac には不要 |
| `wgc.py` | WGC と D3D11 を使う。画素は BGRA。staging テクスチャのリング 6 枚（`RING_SIZE` 45）に読み戻し、タイムスタンプは `SystemRelativeTime`（203-209） | mac では、Swift 補助プログラムと共有メモリのリングで同じインターフェースを実装する（§5.2） |
| `loopback.py` | WASAPI のプロセスループバック。対象はブラウザ本体の PID とその子プロセス。48kHz／16bit／2ch で、時刻は QPC（110-178） | mac では Core Audio のプロセスタップを使う（§5.5） |
| `audiosession.py` | 全出力デバイスのミュートと復元。元の状態は JSON に保存し、異常終了した場合は次の起動時に復元する | mac ではタップの muteBehavior で代わりにする。システムの音量には触れない |
| `browser.py` | `winreg` と Program Files のパスでブラウザを探す。`CREATE_NO_WINDOW` で起動する。PID は PowerShell で取得する（70-85）。HWND をクラス名（`Chrome_WidgetWin_1`、`Chrome_RenderWidgetHostHWND`、160 と 170）で探し、`SetWindowPos` でサイズを合わせ、DWM で枠のずれを求め、`WM_CLOSE` で閉じる | mac ではブラウザを .app のパスで探す。ウィンドウの特定は補助プログラムが PID で絞り込んで行う。サイズ合わせと位置のずれは CDP で求める |
| `recorder.py` | HWND を直接渡している（202、292）。音声は名前付きパイプ `AudioPipe` で FFmpeg に渡す（50-82）。ほかに `CREATE_NO_WINDOW`（226）があり、`perf_counter` を QPC と同じ時計とみなしている（230） | これらを抽象化する。mac では音声に `os.mkfifo` を使い、時計は §5.3 に従う |
| `ffmpeg.py` | `CREATE_NO_WINDOW`、`.exe` の探索（34-43）、`\` を `/` に置き換える処理（68）、入力の `-pix_fmt bgra` が固定（172） | 探索先を OS ごとに分け、入力の画素形式を引数にする |
| `config.py` | `%LOCALAPPDATA%` と `~/Videos` を使う | mac では `~/Library/Application Support/MultiCapture` と `~/Movies/MultiCapture` を使う |
| `gui.py` | テーマ `vista`、フォント `Yu Gothic UI`、`os.startfile`（379、627）、Windows 11 のビルド番号チェック（706）、`ffmpeg.exe` や Edge という文言 | mac ではテーマ `aqua`、ヒラギノ系フォント、`open` コマンドを使う。起動時の環境チェックは §6.1 |
| `build.ps1` | PyInstaller（Windows）と gyan.dev の FFmpeg | mac 用に `build_mac.sh` を新しく作る（§9） |

**変更せずに使えるもの**

- `cdp.py` と `player.py`（全体）
- `splitjob.py` のロジックの大半（区間の計算、つなぎ目の探索、結合）
- `recorder.py` のタイムライン管理、コマのペース配分、音声の配置（`place()`）
- `ffmpeg.py` の `concat`、`decode_gray`、`find_join`

**未使用のコード**

`win32.process_tree`、`WindowCapture.write_to`、`ProcessLoopback.read` は呼ばれていない。リファクタのときに削除してよい。

## 5. 設計方針

### 5.1 OS 依存部分の分離

OS に依存する処理をインターフェースの後ろに移し、`splitjob.py` と `recorder.py` が OS を意識しないようにする。ディレクトリ構成の一例を示す（名前は変えてよい）。

```
multicapture/
  platform/__init__.py   # sys.platform で windows / mac を選ぶ
  platform/base.py       # インターフェース定義
  platform/windows/      # 既存の win32 / wgc / loopback / audiosession と、browser の Windows 部分
  platform/mac/          # mac の実装（Python 側）
native/mac/              # Swift 補助プログラムのソース
```

インターフェースは次の単位で切る。

| インターフェース | 内容 |
|---|---|
| `Clock` | `now()` は秒を返す。キャプチャのタイムスタンプと同じ時間軸を使う（§5.3） |
| `WindowCapture` | いま `recorder.py` が `wgc.WindowCapture` に対して使っているメソッドを、そのままインターフェースにする。対象は `start`、`set_output`、`set_region`、`update`、`seq`、`ring_times`、`ring_seq`、`head`、`slot_at`、`first_seq_after`、`oldest_seq`、`slot_of_seq`、`write_slot(sink, slot)`、`close`。これに `pix_fmt`（`"bgra"` または `"nv12"`）を加える |
| `AudioCapture` | `read_timed()` は（s16le・48kHz・ステレオの bytes, 秒単位のタイムスタンプ）を返す |
| 録画中の消音 | Windows では `SpeakerMute` を使う。mac では `AudioCapture` に「タップ側で消音する」という指定を渡して実現する。`splitjob.py` が OS の違いを知らずに済む形にする |
| ブラウザ操作 | 実行ファイルの検出、起動オプション、キャプチャ対象の特定、内容領域のサイズ合わせ、位置のずれの取得、最小化からの復帰、終了 |
| 音声の入力路 | FFmpeg に音声を渡す経路。Windows は名前付きパイプ、mac は FIFO（`os.mkfifo`）を使う |
| `KeepAwake` | `acquire(keep_display_on)` と `release()` |
| パスと OS 操作 | `data_dir`、既定の出力先、FFmpeg の候補パス、フォルダを開く処理 |

**推奨（任意）**：最小化からの復帰とウィンドウのサイズ・位置合わせは、CDP（`Browser.getWindowForTarget` と `Browser.setWindowBounds`）を使えば Windows と mac で共通にできる。

### 5.2 Swift 補助プログラム（仮称 `mc-capture`）

**役割**：1 つのウィンドウの映像と、1 つのブラウザの音声をキャプチャし、Python に渡す。Python 側の録画ロジック（コマのペース配分、音声の配置、FFmpeg の制御）は Windows と共通のものを使う。

**プロセスの単位**：録画 1 本につき 1 プロセスを推奨する。分ければ、1 本の不具合がほかに波及しない。別の構成を選ぶ場合は理由を報告すること。

**制御**：Python が起動し、stdin と stdout で JSON Lines をやり取りする。stdin が閉じたら（親プロセスが終わったら）、すぐに後始末をして終了する。コマンドの例を示す。

- `check-permission`：画面収録の許可があるかを返す。
- `list-windows {pid}`：その PID のウィンドウ ID、サイズ、タイトルを返す。
- `start {window_id, crop, out_w, out_h, fps, shm_path, audio:{pids, mute}, audio_path}`：キャプチャを始める。
- `stop`：キャプチャを止める。
- 補助プログラムからのイベント：`started`、`error`、`stats`（受け取ったコマ数、空白のコマ、音声の欠けなど）。

**映像**

- ScreenCaptureKit の `SCContentFilter(desktopIndependentWindow:)` を使う。
- 画素形式は `420v`（NV12、ビデオレンジ）にする。
- 出力サイズは `width`/`height` で指定し、`minimumFrameInterval` は 1/fps とする。カーソルは表示しない。
- 切り取りは `sourceRect` で行う。
- 使うのは `SCFrameStatus.complete` のコマだけとする。

**共有メモリのリング**

- 一時フォルダにファイルを作り（権限は 0600）、mmap で共有する。スロットは 6 個（Windows の `RING_SIZE` と同じ）とする。
- 各スロットにヘッダーを置く。内容は seq、タイムスタンプ（float64、§5.3 の時間軸）、幅、高さ、行の長さ（stride）、書き込み中フラグ。
- 読み書きが衝突してコマが壊れないよう、seqlock 方式で守る。

**音声**

- §5.5 のタップで取得し、48kHz・s16le・ステレオに変換する。
- 映像の経路とは別の FIFO に、ヘッダー（タイムスタンプ float64 と長さ uint32）を付けた塊で送る。

**安定性**

- 複数の補助プログラムは、開始を 0.5 秒程度ずつずらす。同時に開始すると replayd が固まるという報告がある。
- コマが一定時間届かなければ、`error` イベントを出す（監視の仕組みを持つ）。

**ログ**：stderr に出し、Python 側で既存のログにまとめる。

### 5.3 時刻の基準

- 補助プログラムは、映像と音声のタイムスタンプをどちらも **mach_absolute_time 系のホスト時刻（秒）** で出す。映像は CMSampleBuffer の PTS、音声は AudioTimeStamp の mHostTime から求める。
- Python 側の mac 用 `Clock` は、ctypes で libSystem の `mach_absolute_time` と `mach_timebase_info` を呼んで実装する。`time.perf_counter()` が同じ時計だと決めつけないこと。CPython は macOS で別の時計を使っている可能性がある。
- 既存のフォールバック処理（ずれが 2 秒以上・5 秒以上の場合の補正）が通常の運用で働くようなら、設計ミスとみなす。働いたときはログに残す。
- ScreenCaptureKit は、画面に変化がないとコマを送ってこない。既存の連続書き込みモードは新しいコマがなければ直前のコマを繰り返すので、この点はそのまま対応できる。

### 5.4 映像の受け渡し形式

- mac では NV12 で受け取り、FFmpeg への入力は `-pix_fmt nv12` とする。
  - 理由：`h264_videotoolbox` は BGRA を受け付けない。BGRA で渡すと CPU での変換が入り、1 本あたり約 0.4 コアを使う（M4 で実測）。データ量も NV12 は BGRA の 37.5% で済む。
- 色空間は BT.709 のビデオレンジとして FFmpeg に伝える。`-color_range tv -colorspace bt709 -color_primaries bt709 -color_trc bt709` を付ける。ブラウザでの表示と色が一致するかは Phase 2 で確認する。
- Windows は BGRA のまま変えない。

### 5.5 ブラウザごとの音声（mac）

- Core Audio のプロセスタップ（`CATapDescription`）で、そのブラウザが音を出しているプロセスだけを録音する。
  - mac の Chrome は、音を本体ではなく補助プロセス（Audio Service のユーティリティプロセス）から出す。どのプロセスを対象にするかは Phase 0 の S2 で決める。
  - 補助プロセスは、ブラウザ本体を親に持ち、`--utility-sub-type=audio.mojom.AudioService` を引数に持つものを `ps` で探す方法が候補になる。
  - Audio Service を本体プロセスで動かす `--disable-features=AudioServiceOutOfProcess` も候補になる。
- 「録画中はPCの音をミュート」がオンのときは、タップの `muteBehavior` を `muted` または `mutedWhenTapped` にする。録音はするがスピーカーには出さない、という動作になる。適用するのは Windows と同じく高速録画のときだけで、オフのとき（およびページの同時録画）は `unmuted` にする。システムの音量やミュート設定には触れない。
- 集約デバイスには、実在する出力デバイスを主デバイスとして入れる。タップだけで集約デバイスを作ると、無音しか取れない。
- 出力デバイスが変わったとき、およびタップが無音（すべて 0）を返し続けるようになったときは、それを検出してタップと集約デバイスを作り直す。後者は macOS 26 系で報告がある。
- `.app` の Info.plist に `NSAudioCaptureUsageDescription`（日本語の説明文）を入れる。

### 5.6 同時録画数の自動決定（Windows と mac で共通）

現在の上限は 8 で固定されている（`config.py:88-89`、`gui.py:196`、`gui.py:310`）。これを次の方式に置き換える。

**計測**

- 実際の録画と同じ入力形式（Windows は bgra、mac は nv12）とエンコーダ設定を使う。
- 合成したコマを 2〜4 本並列で数秒間エンコードし、全体のピクセル処理速度（pixels/s）を測る。
- 合成のコマは事前に作って使い回す。入力の生成が足かせになると、正しく測れない。

**上限**：次の値の最小値とする。

- `floor(0.85 × 計測したピクセル処理速度 ÷ (幅 × 高さ × fps))`
- エンコーダの本数上限
  - NVENC は 12 とする。古いドライバでは 8 なので、セッションの作成に失敗したらその本数より少なく抑える。
  - VideoToolbox でセッション作成エラー（-12915 や -12908）が出た場合も、同じように扱う。
- メモリによる上限：`floor((物理メモリ GB − 4) ÷ 1)`。ブラウザ 1 つで約 1GB を使う前提の目安で、実装時に調整してよい。
- 絶対的な上限：16

**計測結果の保存**：PC、エンコーダ、FFmpeg のバージョンの組ごとに、`data_dir` にキャッシュする。

- 初回は録画を始める前に自動で計測する（数秒かかり、進み具合を表示する）。
- 設定画面に「再計測」ボタンを置く。
- 「同時に録画する数」で選べる最大値は、この上限とする。

**ページを同時録画**：ページごとに解像度と fps が違うので、Σ(幅 × 高さ × fps) が「0.85 × 計測値」を超える構成で開始しようとしたら警告する（開始は止めない）。

**実行中の監視**：エンコーダが追いつかずに捨てたコマ、およびリングから溢れたコマの数を数え、ログと完了画面に表示する。

**参考（実測）**：M4（無印）で 1080p・30fps の H.264 なら、上限は 7 本になる（付録 A）。

### 5.7 mac のエンコーダ設定

- 自動選択の順番は `h264_videotoolbox`、次に `libx264` とする。
- `-allow_sw 0` を付け、ソフトウェアエンコードに切り替わらないようにする。
- 画質は Windows 版（CQ/CRF 23 相当）と見た目が同等になるようにする。具体的な値（`-q:v` を使うか、ビットレートで指定するか）は実装時に比べて決める。
- 区間ファイルの設定（`-bf 0`、`-force_key_frames` で `lead` の位置にキーフレームを置く）が VideoToolbox でも効いているか、つなぎ目の処理で確認する。
- `-power_efficient 1` は使わない。処理能力が半分になる（M4 で実測）。

## 6. 機能要件（mac）

### 6.1 起動とその環境

- macOS 15 未満、または Apple Silicon 以外の Mac では、メッセージを出して終了する。
- 画面収録とシステムオーディオ録音の許可があるかを確認する。なければ、設定する場所（システム設定 →「プライバシーとセキュリティ」→「画面収録とシステムオーディオ録音」）を日本語で案内する。
- ブラウザは `/Applications` と `~/Applications` から探す。対象は `Google Chrome.app` と `Microsoft Edge.app`。mac での既定は Chrome とする。
- FFmpeg は次の順で探す。.app に同梱したもの、アプリと同じ場所、`/opt/homebrew/bin`、`/usr/local/bin`、PATH。Finder から起動した .app では PATH に Homebrew が含まれないため、明示的に探す必要がある。
- データ（設定、ブラウザのプロフィール、ログ）は `~/Library/Application Support/MultiCapture` に置く。出力先の既定は `~/Movies/MultiCapture` とする。

### 6.2 動画を高速録画

Windows 版と同じ動作にする。区間の分割、境目の重ね録り、画像の照合によるつなぎ目の決定、時刻に基づく音声の配置、読み込みが止まったときの巻き戻し、ログイン情報の引き継ぎ、「動画部分だけを録画する」の各機能を含む。mac で違う点は次のとおり。

- **録画中のミュート**：録画用ブラウザの音だけをタップで消す（§5.5）。異常終了したときの復元処理は不要。プロセスが終われば、タップも一緒に消える。
- **スリープの防止**：録画中は、ディスプレイとシステムのスリープを常に止める。親プロセスが死んだら自動で解除される方法を使う（例：`caffeinate -d -i -w <自分のPID>`）。「録画中は画面をオフにしない」のチェックは、mac では常にオンのまま変更できないようにし、その理由を表示する。
- **ウィンドウの重なり**：Phase 0 の S1 で問題がなければ、ウィンドウが重なったままでも録画できるようにする。「最小化しないでください」という注意は Windows と同じ。

### 6.3 ページを同時録画

Windows 版と同じ動作にし、ページごとのプロフィールも保持する。同時録画数の警告は §5.6 に従う。

### 6.4 GUI

- テーマは `aqua`、フォントはヒラギノ系（`Hiragino Sans`）にする。
- フォルダを開く処理は `open` コマンドで行う。
- `ffmpeg.exe` や Edge といった文言は、OS に合わせて切り替える。
- ウィンドウを閉じたとき、または Cmd+Q で終了したときは後始末をする。録画中なら確認を出す。

## 7. Windows 側の変更要件

1. 共通化のためのリファクタを行う（§5.1）。動作は変えない。ファイルの移動と、薄いアダプタを挟むことは許可する。Windows 専用のロジックそのものは書き換えない。
2. 同時録画数の上限を、固定の 8 から §5.6 の方式に変える。NVENC は 12 本まで認める。
3. README の上限の説明（`README.md:34` 付近）を新しい方式に合わせて更新する。
4. 保留（任意）：QSV に `-low_power 1` を付けると、ブラウザの描画との競合が減るという報告がある。ただし古い内蔵 GPU との互換性を確かめる必要があるため、今回は採用しない。

## 8. 非機能要件

| 項目 | 要件 |
|---|---|
| 音ズレ | 0.04 秒以内（Windows 版と同等） |
| つなぎ目 | コマの欠けや重複がないこと |
| 取りこぼし | 自動決定した上限以内の本数なら、捨てるコマは 0 |
| 安定性 | M4（無印）で 7 本・2 時間の動画を録画し、エラーなく完了すること |
| CPU 負荷 | M4 で 7 本を録画中の CPU 使用率を測って報告する。目安は 50% 以下で、超えたら原因を分析する |
| 後始末 | アプリの終了時や異常終了時に、補助プログラム、ブラウザ、caffeinate が残らないこと。残ってしまった場合は、次の起動時にプロフィールのパスから見つけて終了させる |
| ログ | 補助プログラムのログを既存のログファイルにまとめる |

## 9. 配布（署名なし）

- PyInstaller の `--windowed` で .app を作る。アーキテクチャは arm64 とする。
- Info.plist の設定
  - `LSMinimumSystemVersion` は 15.0 にする。
  - `NSAudioCaptureUsageDescription` を入れる。
  - `CFBundleIdentifier` は一度決めたら変えない。変えると、ユーザーが付けた許可が外れる。
- 補助プログラムと FFmpeg は .app の中に同梱する。
- 署名
  - .app に含まれるすべての Mach-O に、内側から順に同じ ID で署名する。基本は ad-hoc 署名とする。
  - **推奨**：開発者がコード署名用の自己署名証明書を 1 つ作り、毎回それで署名する。こうすると、アップデート後も画面収録の許可が残る見込みがある（Phase 0 の S8 で確認する）。Gatekeeper の警告は消えない。証明書と秘密鍵はリポジトリに入れない。
- ZIP は `ditto -c -k --keepParent` で作り、署名と属性を保つ。ファイル名は `MultiCapture-<version>-macos-arm64.zip` とする。
- FFmpeg は、arm64 の静的ビルドで `h264_videotoolbox` を含むものを同梱する。入手元の URL とハッシュをビルドスクリプトに記録し、GPL のライセンス文も同梱する。
- ビルドスクリプトは `build_mac.sh` とし、Swift のビルド（`swiftc`、対象は `arm64-apple-macos15.0`）、PyInstaller、署名、ZIP 作成までを行う。
- README の mac 節に書くこと
  - 初回の開き方（macOS 15 以降）：一度開こうとしてから、システム設定 →「プライバシーとセキュリティ」→「このまま開く」を押す。
  - 許可の付け方（画面収録とシステムオーディオ録音）
  - アップデートしたら許可を付け直す必要があること
  - 月に 1 回程度、許可の再確認ダイアログが出ること
  - 録画中は画面が消えないこと。ロックと最小化をしないこと
  - 同時に録画できる数の目安
  - ソースから起動する方法（`python3 -m multicapture`。Tk を含む Python が必要で、この場合は Terminal に許可を与える）

## 10. フェーズと受け入れ基準

### Phase 0：検証（Go か No-Go かの判断材料を作る）

検証用のコードは `tools/mac-spike/` に置く。配布物には含めない。ユーザーの Mac（M4、macOS 27）で行い、可能なら macOS 15 でも確認する。

| ID | 確認すること | 合格の条件 | 不合格のときの代案 |
|---|---|---|---|
| S1 | 他のウィンドウで完全に隠れた Chrome が描画を続けるか。既存のフラグと候補のフラグを組み合わせて試す | Chrome を 8 個重ねて 10 分間動かし、どのウィンドウでも新しいコマが 29fps 以上届く。SCK の complete のコマ数と、CDP で測る rAF の回数の両方で確認する | ①別のフラグを探す。②ウィンドウを重ならないように並べる（同時録画数が画面の広さで制限される）。どちらにするかはユーザーが判断する |
| S2 | 8 個の Chrome から、それぞれの音声を個別に取れるか | 各ブラウザに周波数の違うテスト音を鳴らさせ、各タップに自分の音だけが入る | `--disable-features=AudioServiceOutOfProcess` を付けて、本体の PID をタップする。それでも駄目ならユーザーが判断する |
| S3 | ミュートしたタップの動作 | 録画用ブラウザの音がスピーカーから出ず、タップには入る。システムの音量が 0 やミュートでも録音できる | — |
| S4 | 時刻の基準と音ズレ | 映像と音声のタイムスタンプが同じ時間軸に乗る。フラッシュとビープのテストページで、音ズレが 40ms 以内（一定のずれなら補正してよい） | 補正値を実測で決める |
| S5 | 8 本のストリームと 8 個のタップを長時間動かしたときの安定性 | 60 分間、取りこぼしや停止がない。開始をずらす効果も確認する | 開始の間隔を調整する。監視して再起動する |
| S6 | Retina 画面での表示、`--force-device-scale-factor=1`、画面より大きいウィンドウ、タイトルバーの切り取り | 1920x1080 を指定すると、ぼやけていない 1920x1080 の映像になる。切り取る位置が正しい | 画面に収まるサイズに抑え、録画後に拡大する（Windows と同じ方式） |
| S7 | ディスプレイのスリープと画面ロック | caffeinate で画面を保っているあいだは、描画もキャプチャも続く。手動でロックしたときや画面が消えたときの動作を記録する | README に制約として書く |
| S8 | 権限と配布 | 隔離属性の付いた ZIP から展開した .app で、①許可のダイアログが MultiCapture の名前で出る、②補助プログラムがその許可を引き継ぐ、③アップデート後に許可がどうなるか（ad-hoc 署名と自己署名証明書で比べる）を確かめる | 結果を README の手順に反映する |
| S9 | ブラウザのプロフィール | `~/Library/Application Support/MultiCapture` の下を user-data-dir にしても Chrome が動く。ログイン情報（Cookie）を引き継げる。キーチェーンのダイアログが出ない | 置き場所を変える。`--use-mock-keychain` と `--password-store=basic` を全インスタンスで揃える |

**成果物**：各項目の合否と数値、採用するフラグと手法をまとめた報告。これをもとにユーザーが Go か No-Go かを判断する。

### Phase 1：共通化と同時録画数の自動決定（Windows の動作は変えない）

- §5.1 のリファクタ、§5.3 の `Clock` の抽象化、§5.6 の自動決定、NVENC の上限 12 への対応を行う。
- mac 上で確認すること
  - すべてのモジュールを import できること（Windows 専用のモジュールが mac で import されないこと）
  - 純粋なロジック（`place()`、`find_join`、上限の計算）を、標準ライブラリの `unittest` でテストすること
- Windows のチェックリスト（ユーザーが実機で確認する）
  1. 動画を高速録画：8 本で行う（NVIDIA の最新ドライバなら 12 本）。つなぎ目、音ズレ、ログインが必要なサイトを確認する
  2. ページを同時録画：2 ページで行う。`--start --duration` も確認する
  3. 手元にあるエンコーダ（NVENC、QSV、AMF、x264）すべてで録画できること
  4. 自動計測の結果が表示されること。再計測できること
  5. 異常終了したあと、次の起動時にスピーカーの状態が元に戻ること

### Phase 2：mac 版「動画を高速録画」（ソースから起動して動けば合格）

- Swift の補助プログラムと、Python 側の mac 用実装を作る。
- 合格の条件（§11 のテスト素材を使う）
  - 2 時間のテスト動画を、M4 で 7 本同時に録画して完了する。
  - つなぎ目でコマが欠けたり重複したりしない。
  - 音ズレが 40ms 以内である。
  - 捨てたコマが 0 である。
  - ウィンドウが隠れていても録画できる。
  - 録画中のミュートが働く。
  - 色がブラウザでの表示と一致する。
- ログインが必要な実在のサイトでの確認は、ユーザーが行う。

### Phase 3：mac 版「ページを同時録画」

- 合格の条件：4 ページを 30 分間録画し、それぞれのファイルが正しく保存される。

### Phase 4：配布物と README

- 合格の条件：新しいユーザーアカウント（またはまっさらな Mac）で ZIP を展開し、README の手順だけで録画まで進めること。macOS 15 と macOS 27 の両方で確認する。

## 11. テスト素材

- **テスト動画**：FFmpeg で作る。
  - 映像には、コマ番号を白黒のブロック模様（バーコード状）で焼き込む。OCR を使わずに読み取れるようにするためである。
  - 1 秒ごとに、白いフラッシュと短いビープを入れる。
  - 長さは 2 時間のものと 5 分のものを用意する。
- **配信**：`<video>` を置いたローカルの HTML を、`python3 -m http.server` で配信する。ログインは不要で、外部のサイトに頼らずに試せる。
- **検証スクリプト**
  - 出力動画からコマ番号を読み取り、欠けや重複を見つける。
  - フラッシュとビープの時刻の差から、音ズレを測る。
  - 標準ライブラリと FFmpeg だけで書く。

## 12. 未確定事項とリスク

| 内容 | 対応 |
|---|---|
| S1 と S2 が不合格になる可能性 | Phase 0 の代案を試す。最終的にはユーザーが判断する |
| 毎月の再許可ダイアログ | 対話して使う分には許容する。README に書く（予約録画はスコープ外） |
| macOS のアップデートで ScreenCaptureKit やタップの挙動が変わる（タップが無音になる報告がある） | 検出して作り直す仕組みと、ログ |
| 署名なしでの配布は、Gatekeeper が今後さらに厳しくなる可能性がある | README に最後の手段として `xattr -dr com.apple.quarantine` を書く |
| NVIDIA の上限 12 が、どのドライバから適用されたか分からない | 自動決定でセッション作成の失敗を検出して対応する |
| Apple の Max／Ultra チップで、エンコード回路の数に比例して性能が伸びるかは未検証 | 自動計測で吸収する |

## 付録 A：同時エンコード数の調査結果

### A.1 ハードウェアごとの目安（1080p・30fps・H.264）

| ハードウェア | ドライバの本数上限 | 回せる本数の目安 | 確度 |
|---|---|---|---|
| NVIDIA GeForce | 12（2025年11月以降のドライバ。それ以前は 8）。上限は GPU ごとではなく PC 全体で数える | 12 | 上限は高い、処理能力は中 |
| Intel UHD 620/630 | なし | 3〜5 | 低〜中 |
| Intel Iris Xe / UHD 770 | なし | 6〜8 | 中 |
| Intel Core Ultra（Arc 系の内蔵 GPU） | なし | 6〜8（データが少ない） | 低 |
| AMD 内蔵 GPU（680M/780M） | なし | 6〜10 | 低 |
| Apple M4（無印）／Pro | 文書化された上限はない | **7（実測）** | 高 |
| Apple Max（エンコード回路 2 基） | 文書化された上限はない | 約 14（推定） | 低〜中 |
| Apple Ultra（エンコード回路 4 基） | 文書化された上限はない | 約 28（推定） | 低 |
| CPU（libx264、8 コア） | — | 2〜4 | 低〜中 |

### A.2 M4 での実測（2026-10-05）

測定環境は FFmpeg 9.0.2、macOS 27.0.1、メモリ 24GB。

- **処理能力**
  - 1080p の H.264（`h264_videotoolbox`、NV12 入力、8Mbps）は、全体で毎秒約 225 コマが上限だった。1 本でも 16 本でも、合計は変わらない。
  - HEVC は約 6% 遅い。
- **実時間での録画（`-re`）**
  - 7 本までは 1.02 倍速で、遅れなくエンコードできた。
  - 8 本では 0.94 倍速に落ち、遅れが出た。
  - 12 本では 0.62 倍速だった。
- **デコードとの関係**：ハードウェアデコードを同時に N 本走らせても、エンコードの速度は変わらなかった。デコードは別の回路で処理されている。
- **入力形式**
  - `h264_videotoolbox` は BGRA を受け付けない。BGRA を渡すと、FFmpeg が CPU で変換を挟み、CPU の使用量が約 7〜8 倍に増える（1 本あたり約 0.4 コア）。
  - `hevc_videotoolbox` は BGRA をそのまま受け付ける。
- **オプション**
  - `-prio_speed 1`、`-realtime 1`、ビットレートの変更は、速度に影響しなかった。
  - `-power_efficient 1` を付けると、約 105 コマ/秒まで半減した。
- **その他**：16 本を同時に開いても、セッションの作成失敗やソフトウェアへの切り替えは起きなかった。エンコーダ 1 本あたりのメモリは約 110MB だった。

## 付録 B：主な参考資料

- ScreenCaptureKit：https://developer.apple.com/videos/play/wwdc2022/10155/ 、https://developer.apple.com/documentation/screencapturekit/scstreamconfiguration
- Core Audio タップ：https://developer.apple.com/documentation/coreaudio/capturing-system-audio-with-core-audio-taps 、https://developer.apple.com/documentation/coreaudio/catapmutebehavior
- タップの実装例：https://github.com/insidegui/AudioCap 、https://github.com/makeusabrew/audiotee 、https://github.com/sbetko/catap
- Chrome の mac でのオクルージョン（隠れたウィンドウの扱い）：https://www.chromium.org/developers/design-documents/mac-occlusion/
- 同時開始で replayd が固まる問題：https://developer.apple.com/forums/thread/772365
- 毎月の再許可：https://developer.apple.com/forums/thread/765103
- 署名と TCC の許可の持続：https://developer.apple.com/forums/thread/795739
- NVENC のセッション上限：https://docs.nvidia.com/video-technologies/video-codec-sdk/13.1/nvenc-application-note/index.html 、https://developer.nvidia.com/video-encode-decode-gpu-support-matrix
- Intel QSV にセッション上限がないこと：https://www.intel.com/content/www/us/en/support/articles/000088556/graphics.html
- FFmpeg の VideoToolbox エンコーダ：https://github.com/FFmpeg/FFmpeg/blob/master/libavcodec/videotoolboxenc.c
