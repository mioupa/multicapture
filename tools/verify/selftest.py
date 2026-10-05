#!/usr/bin/env python3
"""Self-test of make_test_video.py + check_recording.py (work files in media/selftest/).

  python3 tools/verify/selftest.py

1. 60 s clip -> (a) re-encode as-is, (b) re-encode scaled to 1280x720: both must PASS.
2. defects via ffmpeg filters (timestamps kept, so only the defects change): drop frames 311-313,
   duplicate frames 611-612, delay audio by 60 ms during 20-30 s: the checker must report exactly these.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import check_recording as cr  # noqa: E402

WORK = os.path.join(HERE, "media", "selftest")
FF = "ffmpeg"
ENC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", "-fps_mode", "passthrough"]
AENC = ["-c:a", "aac", "-b:a", "128k"]


def ffmpeg(*args):
    r = subprocess.run([FF, "-hide_banner", "-loglevel", "error", "-y", *args])
    if r.returncode:
        raise SystemExit(f"ffmpeg failed: {args}")


def main():
    os.makedirs(WORK, exist_ok=True)
    src = os.path.join(WORK, "src60.mp4")
    subprocess.run([sys.executable, os.path.join(HERE, "make_test_video.py"), "--duration", "60", "--out", src],
                   check=True, stderr=subprocess.DEVNULL)
    results = []

    def expect(name, cond, detail=""):
        results.append(cond)
        print(f"  [{'ok' if cond else 'NG'}] {name} {detail}")

    # (a) as-is re-encode, (b) scaled
    a = os.path.join(WORK, "reenc.mp4")
    ffmpeg("-i", src, *ENC, *AENC, a)
    ra = cr.check(a, 30, 60)
    print(cr.summarize(ra))
    expect("re-encode passes", ra["pass"])
    b = os.path.join(WORK, "reenc720.mp4")
    ffmpeg("-i", src, "-vf", "scale=1280:720", *ENC, *AENC, b)
    rb = cr.check(b, 30, 60)
    expect("720p re-encode passes", rb["pass"], f"(frames {rb['video']['frames_read']}, undecoded {len(rb['video']['undecoded'])})")

    # defects: 60 fps timeline with every frame doubled (timestamps n/30 and n/30+1/60); keep the first copy,
    # except drop originals 311-313 (n 622..627) and keep both copies of 611-612 (n 1222..1225)
    vf = "fps=60,select='(eq(mod(n,2),0)*not(between(n,622,627)))+between(n,1222,1225)'"
    af = ("[0:a]asplit=3[x][y][z];[x]atrim=0:20,asetpts=PTS-STARTPTS[a1];"
          "[y]atrim=20:30,asetpts=PTS-STARTPTS,adelay=60:all=1,atrim=duration=10,asetpts=PTS-STARTPTS[a2];"
          "[z]atrim=30,asetpts=PTS-STARTPTS[a3];[a1][a2][a3]concat=n=3:v=0:a=1[aout]")
    d = os.path.join(WORK, "defects.mp4")
    ffmpeg("-i", src, "-filter_complex", f"[0:v]{vf}[vout];{af}", "-map", "[vout]", "-map", "[aout]", *ENC, *AENC, d)
    rd = cr.check(d, 30, 60)
    print(cr.summarize(rd))
    v, av = rd["video"], rd["av"]
    expect("missing = 311-313", cr.ranges(v["missing_numbers"]) == "311-313" and v["missing_count"] == 3, f"({cr.ranges(v['missing_numbers'])})")
    expect("duplicates = 611,612", v["dup_count"] == 2 and sorted(e["frame"] for e in v["dup_events"]) == [611, 612])
    expect("no backwards jumps", not v["backward_events"])
    g = av["outliers"]
    expect("one A/V outlier group 20-30 s, +60 ms", len(g) == 1 and av["outlier_count"] == 10 and 19.5 < g[0]["from"] < 21.5
           and 28.5 < g[0]["to"] < 30.5 and 55 <= g[0]["median_ms"] <= 65, f"({[(round(x['from'], 2), round(x['to'], 2), x['n'], round(x['median_ms'], 1)) for x in g]})")
    expect("defect clip FAILs", not rd["pass"])
    print("SELFTEST " + ("PASS" if all(results) else "FAIL"))
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
