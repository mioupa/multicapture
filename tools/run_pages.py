"""Run 「ページを同時録画」 without the GUI (for testing, e.g. where tkinter is missing).

    python3 tools/run_pages.py URL [URL ...] --seconds 120 [--size 1920x1080] [--fps 30] [--encoder auto]

Each URL gets its own browser profile under a temporary folder and is recorded to the output
folder from the settings, like the GUI does. Ctrl+C stops the recording early.
"""
import argparse
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from multicapture import ffmpeg  # noqa: E402
from multicapture import platform as osp  # noqa: E402
from multicapture.browser import BrowserWindow, available_browsers  # noqa: E402
from multicapture.config import Settings, Slot  # noqa: E402
from multicapture.recorder import SlotRecorder  # noqa: E402

CASCADE_STEP = 48


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("urls", nargs="+")
    ap.add_argument("--seconds", type=float, default=60)
    ap.add_argument("--size", default="1920x1080")
    ap.add_argument("--fps", type=int, default=None)
    ap.add_argument("--encoder", default="auto")
    ap.add_argument("--browser", default=None)
    args = ap.parse_args()
    w, h = (int(v) for v in args.size.lower().split("x"))

    settings = Settings.load()
    if args.fps:
        settings.fps = args.fps
    if osp.NAME == "mac":
        perms = osp.check_permissions()
        if not osp.permissions_ok(perms):
            perms = osp.request_permissions()
        print(f"permissions: {perms}", flush=True)
    ffmpeg_path = ffmpeg.find_ffmpeg()
    encoder = ffmpeg.detect_encoder(ffmpeg_path, args.encoder)
    exe = available_browsers().get(args.browser or settings.browser) or next(iter(available_browsers().values()))
    print(f"encoder: {encoder}  ffmpeg: {ffmpeg_path}  browser: {exe}", flush=True)

    root = tempfile.mkdtemp(prefix="mc-pages-")
    browsers, recorders = [], []
    keep_awake = osp.KeepAwake("MultiCapture: run_pages")
    try:
        for i, url in enumerate(args.urls):
            b = BrowserWindow(exe, os.path.join(root, f"p{i + 1}"), url, 20 + CASCADE_STEP * i, 20 + CASCADE_STEP * i, w, h)
            b.launch()
            print(f"page {i + 1}: content {b.fit_content(w, h)}", flush=True)
            browsers.append(b)
        keep_awake.acquire(True)
        for i, b in enumerate(browsers):
            r = SlotRecorder(i, Slot(f"page{i + 1}", args.urls[i], w, h), b, settings, ffmpeg_path, encoder,
                             lambda idx, text: print(f"[{idx + 1}] {text}", flush=True))
            r.start()
            recorders.append(r)
        try:
            end = time.monotonic() + args.seconds
            while time.monotonic() < end and all(r.running for r in recorders):
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\n停止しています…", flush=True)
        for r in recorders:
            r.stop()
        for r in recorders:
            r.join(90)
        for r in recorders:
            print(f"page {r.index + 1}: {r.output_path} error={r.error} frames={r.frames_written} "
                  f"dropped={r.dropped_frames} ring_overflow={r.ring_overflow} audio_dropped={r.audio_dropped}", flush=True)
        return 0 if all(not r.error for r in recorders) else 1
    finally:
        keep_awake.release()
        for b in browsers:
            try:
                b.close(timeout=5)
            except Exception:
                pass
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
