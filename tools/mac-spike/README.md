# mac-spike（Phase 0 検証用。配布物には含めない）

`docs/mac-port-requirements.md` §10 Phase 0（S1〜S9）の検証コード。ここにあるコードは本体（`multicapture/`）から import しない。逆に、ここから `multicapture.cdp` を import して使うのはよい。

## 構成

```
tools/mac-spike/
  helper/main.swift        Swift 補助プログラム（mc-spike）
  helper/Info.plist        MCSpike.app の Info.plist
  build_helper.sh          build/MCSpike.app を作って署名する
  spikelib/                Python の共通部品（標準ライブラリのみ）
    chrome.py              Chrome の起動・CDP・プロセス探索・終了
    helper.py              mc-spike の起動と JSON Lines の受信
    analyze.py             WAV 読み込み、Goertzel、フラッシュとビープの検出、統計
    server.py              ローカル HTTP サーバー（media 配信、Cookie 試験用エンドポイント）
    machclock.py           ctypes で mach_absolute_time を読む
    shmring.py             共有メモリのリングの読み手（seqlock の検証）
  media/make_media.py      テスト動画を FFmpeg で作る
  media/player.html        再生ページ（rAF / rVFC の計数、WebAudio のテスト音）
  s1_occlusion.py ...      各検証の実行スクリプト（s1〜s9）
  build/  media/*.mp4  results/   生成物（git 管理外）
```

実行結果は `results/<ID>-<YYYYmmdd-HHMMSS>/` に置き、最後に要約を標準出力へ出す。要約は 30 行程度に収める（生ログはファイルへ）。

## 署名と許可

- バンドル ID は `dev.multicapture.spike`、表示名は `MC Spike`。
- `build_helper.sh` は、環境変数 `MC_SPIKE_KEYCHAIN`、`MC_SPIKE_KEYCHAIN_PASS`、`MC_SPIKE_SIGN_HASH` があれば、その自己署名証明書で署名する。なければ ad-hoc 署名にする。自己署名なら指定要件が「識別子＋証明書」になるので、再ビルドしても画面収録の許可が残る見込み。
- 画面収録とシステムオーディオ録音の許可は、`MCSpike.app` 自体に付ける（この環境では各プロセスが自分自身の責任プロセスになるため）。
- **許可がない状態で `windows` や `capture` を実行しない。** SCShareableContent を呼んだ時点で許可ダイアログが出る。許可は `perm --request` でまとめて求める。

## mc-spike の仕様

起動：`build/MCSpike.app/Contents/MacOS/mc-spike <command> [options]`

- 標準出力には 1 行に 1 つの JSON オブジェクト（JSON Lines）を出す。どの行にも `"ev"` を入れ、`--label` があれば `"label"` も入れる。
- 標準エラーにはログを自由な形式で出す。
- 時刻はすべて「mach_absolute_time 系のホスト時刻（秒、float64）」で表す。

### perm [--request]

`{"ev":"perm","screen":true,"audio":"granted|denied|unknown"}`

- screen は `CGPreflightScreenCaptureAccess()`。
- audio は TCC の SPI（`TCCAccessPreflight("kTCCServiceAudioCapture")` を dlopen で呼ぶ）。0=granted、1=denied、それ以外=unknown。
- `--request` を付けると、足りない方を `CGRequestScreenCaptureAccess()` と `TCCAccessRequest` で求め、結果を待ってから（最大 60 秒）もう一度出力する。

### clock

`{"ev":"clock","mach_abs":..,"cm_host":..,"mach_cont":..,"uptime":..}`

- 間を空けずに読む。`cm_host` は `CMClockGetTime(CMClockGetHostTimeClock())` の秒。

### windows [--pid P]...

1 ウィンドウにつき 1 行出す：`{"ev":"window","window_id":N,"pid":P,"bundle_id":"..","app":"..","title":"..","frame":[x,y,w,h],"on_screen":bool,"layer":n,"active":bool}`。最後に `{"ev":"done","count":n}` を出す。

- `SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)` を使う。
- `--pid` を指定しなければ全ウィンドウを出す。

### audio-procs

1 プロセスにつき 1 行出す：`{"ev":"audio_proc","object_id":N,"pid":P,"bundle_id":"..","running_output":bool,"running_input":bool}`。最後に `done` を出す。

- `kAudioHardwarePropertyProcessObjectList` を使う。TCC は不要。

### capture [options]

映像と音声のどちらか、または両方をキャプチャする。

終了するのは次のいずれかのとき：`--duration` の経過、標準入力の EOF、SIGINT、SIGTERM。終了時は後始末（ストリームの停止、IOProc の停止、集約デバイスとタップの破棄）をし、`{"ev":"stopped","totals":{...}}` を出して終了コード 0 で終わる。致命的なエラーのときは `{"ev":"error","where":"..","msg":"..","code":n}` を出して終了コード 1 で終わる。

**映像**（`--window-id` があるときだけ有効）

| オプション | 内容 |
|---|---|
| `--window-id N` | SCWindow の windowID。`SCContentFilter(desktopIndependentWindow:)` を使う |
| `--size WxH` | 出力のピクセル数（`width`/`height`）。必須 |
| `--fps F` | 既定 30。`minimumFrameInterval = 1/F` |
| `--crop x,y,w,h` | `sourceRect`（ウィンドウ内の座標、ポイント） |
| `--pixfmt nv12\|bgra` | 既定 nv12（`420v`） |
| `--capture-resolution automatic\|best\|nominal` | 既定 automatic |
| `--queue-depth N` | 既定 6 |
| `--frame-log PATH` | complete のコマごとに CSV を 1 行書く（下記） |
| `--dump-dir DIR` と `--dump-every N` または `--dump-seqs a,b,..` | 指定したコマを PNG で保存する（`seq.png`） |
| `--shm PATH` | 共有メモリのリングに書く（下記） |
| `--watchdog SEC` | 既定 2.0。complete のコマが SEC 秒届かないと `{"ev":"stall"}` を、再開したら `{"ev":"resume","gap":秒}` を出す |

- `showsCursor=false`、`colorMatrix` は ITU-R 709、`scalesToFit=true`。
- 使うのは `SCFrameStatus.complete` のコマだけ。ほかの状態は stats で数える。
- 最初の complete のコマで `{"ev":"first_frame","w":..,"h":..,"stride0":..,"content_rect":[..],"content_scale":..,"scale_factor":..,"matrix":"..","primaries":"..","transfer":".."}` を出す。後半の 3 つは CVImageBuffer の添付情報。

frame-log の列：`seq,pts,display_time,arrival,w,h,stride0,cx,cy,cw,ch,content_scale,scale_factor,luma`

- `pts`：CMSampleBuffer の PTS（秒）
- `display_time`：SCStreamFrameInfo.displayTime をホスト時刻の秒に直したもの
- `arrival`：コールバック時の mach_absolute_time（秒）
- `luma`：Y 平面を縦横 16 画素おきに抜き出した平均（0〜255）。bgra のときは BT.709 の係数で求める

**音声**（`--audio-pid` か `--audio-tree` があるときだけ有効）

| オプション | 内容 |
|---|---|
| `--audio-pid P` | 対象の PID（繰り返し指定できる） |
| `--audio-tree P` | P とその子孫すべて（sysctl の KERN_PROC_ALL で ppid をたどる）のうち、オーディオのプロセスオブジェクトがあるもの |
| `--audio-wait SEC` | 既定 10。対象のオブジェクトが 1 つもなければ 0.25 秒おきに探し直す |
| `--mute unmuted\|muted\|mutedWhenTapped` | 既定 unmuted |
| `--audio-out PATH` | s16le の PCM WAV（タップの元のサンプルレートとチャンネル数のまま） |
| `--audio-log PATH` | IOProc の呼び出しごとに CSV を 1 行書く：`host_time,sample_time,frames,now,peak` |

- タップは `CATapDescription(stereoMixdownOfProcesses:)` で作る。`isPrivate=true`。
- 集約デバイスは private にする。主デバイスは既定の出力デバイスとし、TapAutoStart=true、タップのドリフト補正をオンにする。
- `host_time` は `inInputTime.mHostTime` を秒に直したもの、`now` は `inNow.mHostTime` を秒に直したもの。
- `started` の中で、対象の PID、オブジェクト ID、サンプルレート、チャンネル数、出力デバイス名を報告する。
- 既定の出力デバイスが変わったら `{"ev":"audio_device_changed"}` を出す（作り直しはしない）。
- 全サンプルが 0 の呼び出しが 5 秒以上続いたら `{"ev":"audio_silent"}` を、戻ったら `{"ev":"audio_unsilent"}` を出す。

**共通**

| オプション | 内容 |
|---|---|
| `--duration SEC` | 既定 0（標準入力の EOF かシグナルまで続ける） |
| `--stats-interval SEC` | 既定 1.0 |
| `--label STR` | すべての行に入れる |

stats の行：`{"ev":"stats","t":..,"complete":n,"idle":n,"blank":n,"suspended":n,"started":n,"stopped":n,"audio_cb":n,"audio_frames":n,"audio_zero_cb":n,"audio_peak":x}`。数値は区間内の値で、累計ではない。

### shm-selftest --shm PATH --size WxH [--fps F] [--duration SEC]

許可なしで共有メモリのリングを試すためのコマンド。`capture` と同じ書き込みコードで、合成した nv12 のコマを書く。Y 平面は全画素を `frame_seq % 256`、UV は 128 にする。stats は `complete` だけを数える。

### 共有メモリのリング（`--shm`）

すべてリトルエンディアン。§5.2 の試作。

- ファイルヘッダー（64 バイト）
  - 0：magic `MCRING01`（8 バイト）
  - 8：u32 version=1
  - 12：u32 slot_count=6
  - 16：u32 slot_size（スロットヘッダーを含む）
  - 20：u32 header_size=64
  - 24：u32 pix_fmt（0=nv12、1=bgra）
  - 28：u32 width
  - 32：u32 height
  - 40：u64 latest_seq（スロットを書き終えたあとに更新する。まだなければ 0）
- スロット i は `64 + i*slot_size` から始まる。スロットヘッダー（64 バイト）
  - 0：u64 seqlock（書き込み中は奇数）
  - 8：u64 frame_seq（1 から始まる）
  - 16：f64 pts
  - 24：u32 width
  - 28：u32 height
  - 32：u32 stride0
  - 36：u32 stride1
  - 40：u32 plane0_off
  - 44：u32 plane1_off
  - 48：u32 data_bytes
- 画素は行の詰め物を除いて詰めて置く。nv12 は stride0=width、stride1=width（UV が交互に並び height/2 行）。bgra は stride0=width*4。
- 書き手の手順：slot = frame_seq % 6。seqlock を +1（奇数）→ メモリバリア → データとフィールドを書く → メモリバリア → seqlock を +1（偶数）→ latest_seq を書く。
- ファイルは 0600 で作り、ftruncate してから mmap する。終了時に unlink はしない（読み手が後で消す）。

## Python 側の約束

- Chrome：`/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`（なければ `~/Applications`）。
- 既定のフラグは `multicapture/browser.py` の `RENDER_FLAGS` を写す。`--disable-features` は Chrome が最後の 1 つしか見ないので、必ず 1 つにまとめる（`spikelib.chrome` で合成する）。
- プロフィールは既定で `results/<run>/profiles/<name>` に作る。S9 だけは `~/Library/Application Support/MultiCapture-spike/` を使う。
- Python は標準ライブラリだけを使う。
