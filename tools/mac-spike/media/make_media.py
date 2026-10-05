#!/usr/bin/env python3
"""Generate the barcode/flash/beep test video with FFmpeg only.

Layout (WxH, default 1920x1080):
  - background dark gray; top band of height round(96*H/1080) split into 24 equal cells
  - cell 0 and 23: white sentinels; cells 1..20: frame number bits LSB first (white=1)
  - cell 21: parity of the 20 bits (white if odd); cell 22: black
  - frames with n % fps in {0,1}: full-frame white flash (drawn BEFORE the barcode)
  - audio: 48 kHz stereo, 1 kHz beep (amp 0.5) for exactly 2 frames at each integer second
"""
import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CELLS = 24
NBITS = 20


def build_filter(w, h, fps):
    band = round(96 * h / 1080)
    cw = w // CELLS
    f = ["format=yuv420p",
         f"drawbox=x=0:y=0:w=iw:h=ih:color=0x202020:t=fill",
         f"drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:enable='between(mod(n,{fps}),0,1)'",
         f"drawbox=x=0:y=0:w={cw * CELLS}:h={band}:color=black:t=fill"]
    white = lambda i: f"drawbox=x={i * cw}:y=0:w={cw}:h={band}:color=white:t=fill"
    f.append(white(0) + "")
    f.append(white(CELLS - 1))
    for k in range(NBITS):
        f.append(white(1 + k) + f":enable='eq(mod(floor(n/{2 ** k}),2),1)'")
    par = "+".join(f"mod(floor(n/{2 ** k}),2)" for k in range(NBITS))
    f.append(white(21) + f":enable='eq(mod({par},2),1)'")
    return ",".join(f)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--duration", type=float, default=10)
    ap.add_argument("--size", default="1920x1080")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--out", default=os.path.join(HERE, "test_10s.mp4"))
    ap.add_argument("--preset", default="veryfast")
    a = ap.parse_args()
    w, h = map(int, a.size.lower().split("x"))
    fps = a.fps
    vf = build_filter(w, h, fps)
    beep = f"0.5*sin(2*PI*1000*t)*lt(mod(t,1),{2 / fps:.9f})"
    cmd = ["ffmpeg", "-hide_banner", "-y", "-loglevel", "error", "-stats",
           "-f", "lavfi", "-i", f"color=c=0x202020:s={w}x{h}:r={fps}:d={a.duration}",
           "-f", "lavfi", "-i", f"aevalsrc='{beep}|{beep}':s=48000:d={a.duration}",
           "-vf", vf,
           "-c:v", "libx264", "-preset", a.preset, "-crf", "20", "-g", "60", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-shortest", a.out]
    sys.exit(subprocess.run(cmd).returncode)


if __name__ == "__main__":
    main()
