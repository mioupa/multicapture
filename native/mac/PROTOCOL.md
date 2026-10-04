# mc-capture：Python とのやり取り

`mc-capture` は ScreenCaptureKit と Core Audio タップで、1 つのブラウザの映像と音声を取り込む Swift の補助プログラム。録画 1 本につき 1 プロセスを起動する。ほかに、ウィンドウの一覧や許可の確認に使う「制御用」のプロセスを 1 つ置いてよい。

要件は `docs/mac-port-requirements.md` の §5.2〜§5.5、Phase 0 の結果は `docs/mac-port-phase0-report.md`、試作は `tools/mac-spike/helper/`。

## 起動

```
mc-capture [--disclaim] serve
```

- 標準入力から命令を、標準出力へイベントを、どちらも JSON Lines（1 行に 1 つの JSON オブジェクト）で流す。標準エラーにはログを出す。
- 標準入力が EOF になったら（親プロセスが死んだら）、すぐに後始末をして終了する。SIGTERM と SIGINT でも同じ。
- `--disclaim`：自分を「責任プロセス」にして起動し直す（`responsibility_spawnattrs_setdisclaim`）。ソースから起動したときに使い、TCC の許可を補助プログラムのバンドル自身で判定させる。.app に同梱したときは付けない（アプリの許可を引き継ぐ）。
- CLI のままでも `NSApplication.shared` を初期化する（`setActivationPolicy(.prohibited)`）。初期化しないと SCStream が `CGS_REQUIRE_INIT` で落ちる。
- 時刻はすべて、mach_absolute_time 系のホスト時刻（秒、float64）で表す。

## 命令（標準入力）

どの命令にも `"id"` を付けてよい。付けた場合は、その命令への返答イベントに同じ `"id"` を入れる。

| 命令 | 引数 | 返答 |
|---|---|---|
| `check_permission` | なし | `permission` |
| `request_permission` | なし | `permission`（ダイアログの結果を最大 60 秒待つ） |
| `list_windows` | `pid` | `windows` |
| `start_video` | `window_id`、`width`、`height`、`crop`:[x,y,w,h]、`fps`、`shm` | `video_started`、続いて `first_frame` |
| `update_video` | `crop`:[x,y,w,h] | `video_updated` |
| `stop_video` | なし | `video_stopped` |
| `start_audio` | `tree_pid`、`mute`、`fifo` | `audio_started` |
| `stop_audio` | なし | `audio_stopped` |
| `quit` | なし | `bye`、その後に終了する |

**start_video の補足**

- `crop` はウィンドウ内の座標（ポイント）で、`sourceRect` に使う。
- 出力は `width`×`height` のピクセル。`captureResolution = .nominal`、`420v`（NV12）、`colorMatrix` は ITU-R 709、カーソルは出さない、`queueDepth` は 6。
- `minimumFrameInterval` は 1 / min(2×fps, 120)。上限を fps ちょうどにすると、同じ fps の動画のコマを取りこぼすため。
- 使うのは `SCFrameStatus.complete` のコマだけ。

**start_audio の補足**

- `tree_pid`（ブラウザ本体の PID）とその子孫のうち、オーディオのプロセスオブジェクトを持つものをタップの対象にする。Chrome では Audio Service のユーティリティプロセスが該当する。
- 見つからなければ、0.25 秒おきに最大 15 秒探す。見つからないまま終わったら `error`（`fatal:false`）を出し、見つかり次第開始する。
- `mute` は `unmuted`、`muted`、`mutedWhenTapped` のいずれか。

## イベント（標準出力）

すべてのイベントに `"ev"` と `"t"`（ホスト時刻）を入れる。

- `ready`：起動直後に出す。中身は `{"version":"…","disclaimed":bool}`。
- `permission`：`{"screen":bool,"audio":"granted|denied|unknown"}`
- `windows`：`{"windows":[{"window_id","pid","title","frame":[x,y,w,h],"on_screen","layer"}]}`
- `video_started`、`first_frame`：`first_frame` には `w`、`h`、`content_rect`、`content_scale`、`scale_factor`、`pts` を入れる。
- `audio_started`：`{"pids":[…],"device":"出力デバイス名","device_rate":…}`
- `stats`：1 秒ごとに出す。`{"complete":n,"idle":n,"other":n,"audio_frames":n,"audio_zero":bool,"shm_skipped":n}`。数値は直近 1 秒の値。
- `stall`、`resume`：映像のコマが 2 秒届かないと `stall` を、届き始めたら `resume`（`gap` 秒を付ける）を出す。
- `audio_rebuilt`：`{"reason":"device_changed|rate_changed|silent_tap|process_changed"}`。タップと集約デバイスを作り直したときに出す。
- `error`：`{"where","msg","code","fatal":bool}`。`fatal:true` のときは、その後に終了する。
- `video_stopped`、`audio_stopped`、`bye`

## 映像：共有メモリのリング

`tools/mac-spike/README.md` の「共有メモリのリング」と同じ配置にする。

- ファイルの先頭は magic `MCRING01`。スロットは 6 個で、各スロットを seqlock で守る。
- 画素は詰めて置く NV12（Y は width バイト × height 行、UV は width バイト × height/2 行）。
- 64 バイトのファイルヘッダーの 40 バイト目（u64 `latest_seq`）は、スロットを書き終えたあとに更新する。
- スロットヘッダーの `plane0_off` と `plane1_off` は、スロットの先頭からのオフセットとする。
- ファイルは Python 側が作るのではなく、`start_video` を受けた補助プログラムが作る（0600、ftruncate、mmap）。後片付け（削除）は Python 側が行う。
- `width`／`height` と違う大きさのバッファが届いたら、拡大縮小はせずにそのコマを捨て、`shm_skipped` に数える。

## 音声：FIFO

- Python 側があらかじめ `os.mkfifo` で作ったパスを、`start_audio` の `fifo` で渡す。補助プログラムは書き込み側として開く。
- 中身はレコードの並びで、1 レコードは次のとおり（リトルエンディアン）。
  - u32 magic `0x5541434D`（"MCAU"）
  - f64 timestamp：先頭サンプルのホスト時刻（秒）
  - u32 frames
  - frames × 4 バイト：s16le、48kHz、ステレオ
- 変換：集約デバイスの実際の入力形式（主デバイスのレートで動く。例：192kHz の float32）を、AVAudioConverter で 48kHz の s16le ステレオにする。
- timestamp は、IOProc の `inInputTime.mHostTime` から求めた、変換前のバッファの先頭時刻とする（変換器の遅延は無視してよい）。
- 書き込みが詰まっても IOProc を止めない。音声は内部のキューを通して別のスレッドで書き、キューが溢れたら古いものから捨てて `stats` で数える。

## 音声の作り直し

次のいずれかが起きたら、タップと集約デバイスを作り直して `audio_rebuilt` を出す。FIFO はそのまま使い続ける。

- 既定の出力デバイスが変わったとき（`device_changed`）
- 主デバイスのサンプルレートが変わったとき（`rate_changed`）
- 対象プロセスが音を出している（`kAudioProcessPropertyIsRunningOutput`）のに、タップが 5 秒以上すべて 0 を返し続けたとき（`silent_tap`）。60 秒あたり 3 回までとする。
- `tree_pid` の子孫に、新しくオーディオのプロセスオブジェクトが現れたか、対象が消えたとき（`process_changed`）。2 秒ごとに調べる。

## テスト用の命令

許可（画面収録・オーディオ）がなくても、共有メモリと FIFO の書き込み側を試せるようにした合成データの命令。本番の `start_video` / `start_audio` と同じ書き込みコードを通る。`stop_video` / `stop_audio`、`stats`、`first_frame` も本番と同じに動く。実際の API には一切触れない。

| 命令 | 引数 | 返答 |
|---|---|---|
| `selftest_video` | `shm`、`width`、`height`（どちらも偶数）、`fps`（省略時 30） | `video_started`、続いて `first_frame` |
| `selftest_audio` | `fifo`、`freq`（省略時 1000） | `audio_started`（`pids:[]`、`device:"selftest"`、`device_rate:48000`） |

- 映像：seq 番目のコマは、Y の (行 r, 列 c) の値が `(seq*7 + r + c) & 255`、UV は偶数バイトが 128、奇数バイトが 64。
- 音声：`freq` Hz、振幅 0.5 のサイン波（48kHz s16 ステレオ、左右同じ）。480 フレーム（10ms）ごとに 1 レコード。timestamp は開始時刻から 1/48000 秒刻み。
- 命令を表すキーは `"cmd"`（`"command"` でも可）。

## 実装メモ（本文への補足）

- `stats` には、本文の項目に加えて `audio_dropped`（直近 1 秒に FIFO のキューから捨てたレコード数）を入れる。`stats` は映像か音声が動いている間だけ出る。
- `stall` の判定には、`complete` に加えて `idle` のコマも「届いた」として数える（静止画面では `complete` が来ないため）。
- `NSApplication` の初期化は、最初に SCK を使う命令（`list_windows`、`start_video`）の直前に行う。
- 15 秒たっても音声のプロセスが見つからなかったときは `error`（`fatal:false`、`start_audio` の `id` 付き）を出し、そのまま 2 秒おきに探し続ける。見つかったら `audio_started` を出す。
- FIFO は、読み側が開くまで 50ms おきに非ブロッキングで開き直す。残りのレコードは終了時に最大 1 秒かけて書き出す。
