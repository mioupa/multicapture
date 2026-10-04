#!/usr/bin/env python3
"""Requirement 5.7 helper: compare libx264 crf 23 (Windows-equivalent) with h264_videotoolbox settings.

  python3 tools/verify/vt_quality.py [--work DIR] [--sources hard,mid] [--q 40,50,60,70,80] [--bitrates 8,12,16]

Builds hard 30 s 1080p30 sources (lossless ffv1 + raw nv12 copy; ~2-3 GB each, kept in --work, default
media/vt_work, which is gitignored), encodes each setting from nv12 input (as the app will), then measures
bitrate, SSIM, PSNR vs the exact (raw nv12) encoder input and the encode speed. Writes <work>/results.json.
  hard = full-frame zooming mandelbrot + testsrc2 inset + cellauto + light noise (crisp, high detail, worst case)
  mid  = blurred testsrc2 + blurred mandelbrot + very light noise (closer to screen/video content)
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
FF = "ffmpeg"
DUR = 30
SRC_GRAPHS = {
    "hard": ('-f lavfi -i mandelbrot=s=1920x1080:r=30:end_scale=0.02 -f lavfi -i testsrc2=s=960x540:r=30:d=30 '
             '-f lavfi -i cellauto=s=480x270:r=30:rule=110:random_fill_ratio=0.5:scroll=1 '
             '-filter_complex [1:v]format=yuv420p[t];[2:v]format=yuv420p,scale=480:270:flags=neighbor[c];'
             '[0:v]format=yuv420p[m];[m][t]overlay=x=40:y=40:shortest=1[a];[a][c]overlay=x=1400:y=700:shortest=1,noise=alls=2:allf=t,format=yuv420p[v]'),
    "mid": ('-f lavfi -i testsrc2=s=1920x1080:r=30:d=30 -f lavfi -i mandelbrot=s=1280x720:r=30:end_scale=0.05 '
            '-filter_complex [1:v]format=yuv420p,gblur=sigma=1.0[m];[0:v]gblur=sigma=0.6[b];'
            '[b][m]overlay=x=320:y=180:shortest=1,noise=alls=1:allf=t,format=yuv420p[v]'),
}


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def build_source(name, work):
    mkv, raw = os.path.join(work, f"src_{name}.mkv"), os.path.join(work, f"src_{name}.nv12")
    if not os.path.exists(mkv):
        run([FF, "-hide_banner", "-loglevel", "error", "-y", *SRC_GRAPHS[name].split(" "), "-map", "[v]", "-t", str(DUR),
             "-c:v", "ffv1", "-level", "3", mkv]).check_returncode()
    if not os.path.exists(raw):
        run([FF, "-hide_banner", "-loglevel", "error", "-y", "-i", mkv, "-pix_fmt", "nv12", "-f", "rawvideo", raw]).check_returncode()
    return mkv, raw


def encode(raw, out, venc):
    t0 = time.time()
    r = run([FF, "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "nv12", "-video_size", "1920x1080",
             "-framerate", "30", "-i", raw, *venc, "-g", "60", "-an", out])
    if r.returncode:
        return None, r.stderr.strip()[-200:]
    return time.time() - t0, ""


def measure(out, raw):
    """vs the exact encoder input (raw nv12, CFR); both sides re-stamped by frame number."""
    r = run([FF, "-hide_banner", "-i", out, "-f", "rawvideo", "-pix_fmt", "nv12", "-video_size", "1920x1080", "-framerate", "30", "-i", raw,
             "-lavfi", "[0:v]format=yuv420p,setpts=N/30/TB,split[a1][a2];[1:v]format=yuv420p,setpts=N/30/TB,split[b1][b2];[a1][b1]ssim;[a2][b2]psnr",
             "-f", "null", "-"])
    ssim = re.search(r"SSIM Y:\S+ \(\S+\) U:\S+ \(\S+\) V:\S+ \(\S+\) All:(\S+)", r.stderr)
    psnr = re.search(r"PSNR y:\S+ u:\S+ v:\S+ average:(\S+)", r.stderr)
    return float(ssim.group(1)) if ssim else None, float(psnr.group(1)) if psnr else None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", default=os.path.join(HERE, "media", "vt_work"))
    ap.add_argument("--sources", default="hard,mid")
    ap.add_argument("--q", default="40,50,55,60,65,70,80")
    ap.add_argument("--bitrates", default="4,8,12")
    a = ap.parse_args()
    os.makedirs(a.work, exist_ok=True)
    settings = [("x264 veryfast crf23", ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p"])]
    vt = ["-c:v", "h264_videotoolbox", "-allow_sw", "0", "-pix_fmt", "nv12"]
    settings += [(f"vt q:v {q}", [*vt, "-q:v", q]) for q in a.q.split(",") if q]
    settings += [(f"vt b:v {b}M", [*vt, "-b:v", f"{b}M"]) for b in a.bitrates.split(",") if b]
    results = {}
    for name in a.sources.split(","):
        mkv, raw = build_source(name, a.work)
        print(f"== source {name}")
        results[name] = []
        for label, venc in settings:
            out = os.path.join(a.work, f"{name}_{label.replace(' ', '_').replace(':', '')}.mp4")
            secs, err = encode(raw, out, venc)
            if secs is None:
                print(f"{label:22s} FAILED {err}")
                continue
            ssim, psnr = measure(out, raw)
            kbps = os.path.getsize(out) * 8 / DUR / 1000
            row = {"setting": label, "kbps": round(kbps), "ssim": ssim, "psnr": psnr, "fps": round(DUR * 30 / secs, 1)}
            results[name].append(row)
            print(f"{label:22s} {kbps:8.0f} kbps  SSIM {ssim}  PSNR {psnr}  {row['fps']} fps", flush=True)
    with open(os.path.join(a.work, "results.json"), "w") as f:
        json.dump(results, f, indent=1)


if __name__ == "__main__":
    main()
