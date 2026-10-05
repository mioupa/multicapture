#!/usr/bin/env python3
"""Generate the barcode/flash/beep test video (requirements section 11) with FFmpeg only.

Layout (WxH, default 1920x1080), robust to H.264 re-encode and scaling:
  - dark gray background (0x202020); two identical barcode bands (top and bottom), each
    round(96*H/1080) px high and split into 24 equal cells
  - cell 0 and 23: white sentinels; cells 1..20: frame number bits, LSB first (white=1)
    cell 21: parity of the 20 bits (white if odd); cell 22: black
  - frames with n % fps in {0,1}: full-frame white flash (drawn BEFORE the bands)
  - audio: 48 kHz stereo AAC, 1 kHz beep (amp 0.5) lasting exactly 2 frames at each integer second
Outputs (default): media/test_5min.mp4 (--duration 300) ; use --duration 7200 --out media/test_2h.mp4.
"""
import argparse
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
MEDIA = os.path.join(HERE, "media")
CELLS = 24
NBITS = 20


def build_filter(w, h, fps):
    band = round(96 * h / 1080)
    cw = w // CELLS
    # Two large boxes move every frame, so neighbouring frames differ clearly even in the small
    # thumbnails the app's join search compares (like real video), not only in the barcode bands.
    bw, bh = round(w / 4), round(h * 0.4)
    sq = round(h * 0.28)
    moving = (f"color=c=0x6080ff:s={bw}x{bh}:r={fps}[b1];color=c=0xff8040:s={sq}x{sq}:r={fps}[b2];"
              f"[in]format=yuv420p[v0];"
              f"[v0][b1]overlay=x='mod(n*{round(w / 31)},W-w)':y={round(h * 0.2)}:shortest=1[v1];"
              f"[v1][b2]overlay=x={round(w * 0.7)}:y='{band}+mod(n*{round(h / 29)},H-2*{band}-h)':shortest=1[v2];[v2]")
    f = [f"drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:enable='between(mod(n,{fps}),0,1)'"]
    for y in (0, h - band):
        f.append(f"drawbox=x=0:y={y}:w={cw * CELLS}:h={band}:color=black:t=fill")
        white = lambda i, y=y: f"drawbox=x={i * cw}:y={y}:w={cw}:h={band}:color=white:t=fill"
        f.append(white(0))
        f.append(white(CELLS - 1))
        for k in range(NBITS):
            f.append(white(1 + k) + f":enable='eq(mod(floor(n/{2 ** k}),2),1)'")
        par = "+".join(f"mod(floor(n/{2 ** k}),2)" for k in range(NBITS))
        f.append(white(21) + f":enable='eq(mod({par},2),1)'")
    return moving + ",".join(f) + "[out]"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--duration", type=float, default=300, help="seconds (default 300)")
    ap.add_argument("--size", default="1920x1080")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--out", default=None, help="default media/test_5min.mp4 or media/test_2h.mp4 by duration")
    ap.add_argument("--preset", default="veryfast")
    ap.add_argument("--crf", default="20")
    ap.add_argument("--ffmpeg", default="ffmpeg")
    a = ap.parse_args()
    w, h = map(int, a.size.lower().split("x"))
    fps = a.fps
    out = a.out or os.path.join(MEDIA, "test_2h.mp4" if a.duration >= 3600 else "test_5min.mp4")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    beep = f"0.5*sin(2*PI*1000*t)*lt(mod(t,1),{2 / fps:.9f})"
    cmd = [a.ffmpeg, "-hide_banner", "-y", "-loglevel", "error", "-stats",
           "-f", "lavfi", "-i", f"color=c=0x202020:s={w}x{h}:r={fps}:d={a.duration}",
           "-f", "lavfi", "-i", f"aevalsrc='{beep}|{beep}':s=48000:d={a.duration}",
           "-vf", build_filter(w, h, fps),
           "-c:v", "libx264", "-preset", a.preset, "-crf", a.crf, "-g", str(fps * 2), "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", "-shortest", out]
    t0 = time.time()
    rc = subprocess.run(cmd).returncode
    if rc == 0:
        print(f"\n{out}: {os.path.getsize(out) / 1e6:.1f} MB in {time.time() - t0:.0f} s", file=sys.stderr)
    sys.exit(rc)


if __name__ == "__main__":
    main()
