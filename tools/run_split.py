#!/usr/bin/env python3
"""Run a SplitJob (動画を高速録画) without the GUI.

    python3 tools/run_split.py URL [--count 4] [--size 1920x1080] [--fps 30] [--encoder auto]
                               [--output-dir DIR] [--mute] [--browser chrome]

Prints events and progress, then the final statistics. Ctrl+C cancels the job and cleans up.
No cookies are passed (log in beforehand is not supported here); the settings file is not modified.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from multicapture import capacity, ffmpeg  # noqa: E402
from multicapture import platform as osp  # noqa: E402
from multicapture.config import Settings, log_dir  # noqa: E402
from multicapture.splitjob import SplitJob, fmt_time  # noqa: E402


def parse_size(text):
    try:
        w, h = text.lower().split("x")
        return int(w), int(h)
    except ValueError:
        raise argparse.ArgumentTypeError("size must look like 1920x1080")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url")
    ap.add_argument("--count", type=int, default=4, help="simultaneous recordings (capped by the measured limit)")
    ap.add_argument("--size", type=parse_size, default=(1920, 1080))
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--encoder", default="auto", choices=["auto", *ffmpeg.ENCODERS])
    ap.add_argument("--output-dir", default=None)
    ap.add_argument("--mute", action="store_true", help="silence the recording browsers (mute while tapped on macOS)")
    ap.add_argument("--no-pin-video", action="store_true")
    ap.add_argument("--browser", default=osp.DEFAULT_BROWSER)
    args = ap.parse_args(argv)

    err = osp.check_environment()
    if err:
        print(err, file=sys.stderr)
        return 1
    browsers = osp.available_browsers()
    exe = browsers.get(args.browser) or next(iter(browsers.values()), None)
    if not exe:
        print("ブラウザが見つかりません", file=sys.stderr)
        return 1
    ffmpeg_path = ffmpeg.find_ffmpeg()
    if not ffmpeg_path:
        print(f"{osp.FFMPEG_NAME} が見つかりません", file=sys.stderr)
        return 1

    settings = Settings.load()
    settings.fps = max(1, min(60, args.fps))
    if args.output_dir:
        settings.output_dir = args.output_dir
    settings.encoder = args.encoder
    if osp.NAME == "mac":
        osp.cleanup_leftovers(os.path.dirname(settings.path))
        try:
            perms = osp.check_permissions()
            print("permissions:", perms)
            if not osp.permissions_ok(perms):
                print("画面収録とシステムオーディオ録音の許可を求めます…")
                perms = osp.request_permissions()
                print("permissions:", perms)
        except Exception as exc:
            print(f"補助プログラムを確認できませんでした: {exc}", file=sys.stderr)
            return 1

    encoder = ffmpeg.detect_encoder(ffmpeg_path, settings.encoder)
    print(f"encoder: {encoder}  ffmpeg: {ffmpeg_path}  browser: {exe}")
    count = max(1, args.count)
    meas = capacity.load(ffmpeg_path, encoder)
    if meas is None:
        try:
            meas = capacity.measure(ffmpeg_path, encoder, capacity.default_pix_fmt(),
                                    lambda f, text: print(f"\r{text} {f * 100:.0f}%   ", end="", flush=True))
            print()
            capacity.save(meas, ffmpeg_path)
        except Exception as exc:
            print(f"\n上限の計測に失敗したため、従来の上限 {capacity.FALLBACK_LIMIT} を使います: {exc}")
            meas = None
    if meas is not None:
        limit, reason = capacity.limit_for(meas, args.size[0], args.size[1], settings.fps, osp.physical_memory_bytes())
    else:
        limit, reason = capacity.FALLBACK_LIMIT, "計測失敗"
    if count > limit:
        print(f"count {count} -> {limit} ({reason})")
        count = limit

    state = {"last_status": None, "done": None, "error": None, "last_line": 0.0}

    def on_event(kind, payload):
        if kind == "status" and payload != state["last_status"]:
            state["last_status"] = payload
            print(f"[status] {payload}", flush=True)
        elif kind == "started":
            print(f"[started] {payload}", flush=True)
        elif kind == "segments":
            now = time.monotonic()
            if now - state["last_line"] >= 2.0:
                state["last_line"] = now
                print("[segments] " + "  ".join(
                    f"#{s['index'] + 1} {s['status']} {s['progress'] * 100:.0f}%" for s in payload), flush=True)
        elif kind == "done":
            state["done"] = payload
            print(f"[done] {payload}", flush=True)
        elif kind == "error":
            state["error"] = payload
            print(f"[error] {payload}", flush=True)

    job = SplitJob(args.url, count, args.size[0], args.size[1], not args.no_pin_video, settings.output_dir, settings,
                   exe, ffmpeg_path, encoder, args.mute, [], on_event)
    job.start()
    try:
        while job.running:
            time.sleep(0.2)
            if osp.NAME == "mac":
                hint = osp.capture_hint(settings.fps)
                if hint and hint != state.get("hint"):
                    print(f"[hint] {hint}", flush=True)
                state["hint"] = hint
    except KeyboardInterrupt:
        print("\n中止しています…")
        job.cancel()
        job.join(120)
    job.join()
    print("stats:", " ".join(f"{k}={v}" for k, v in job.stats.items()))
    if job.duration:
        print(f"video length: {fmt_time(job.duration)}")
    print(f"logs: {log_dir()}")
    return 0 if state["done"] else 1


if __name__ == "__main__":
    sys.exit(main())
