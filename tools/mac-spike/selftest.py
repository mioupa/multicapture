#!/usr/bin/env python3
"""Offline self-tests for spikelib (no TCC needed): analysis, shm ring, media, server Range."""
import argparse
import math
import os
import random
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from spikelib import analyze, shmring, server, helper, MEDIA_DIR  # noqa: E402

fails = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {info}" if info else ""))
    if not cond:
        fails.append(name)


def test_analysis(tmp):
    rate, T0, off = 48000, 1000.0, 2 / 60.0 * 1.0  # injected: beep 2 frames (33.33 ms) after flash
    dur = 20
    # beeps: 1 kHz amp 0.5, 2/60 s long at every integer second (+off after flash edge)
    n = rate * dur
    pcm = [0] * n
    for k in range(1, dur - 1):
        s0 = int((k + off) * rate)
        for i in range(int(rate * 2 / 60)):
            pcm[s0 + i] = int(16384 * math.sin(2 * math.pi * 1000 * i / rate))
    wavp = os.path.join(tmp, "x.wav")
    with wave.open(wavp, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(struct.pack(f"<{n}h", *pcm))
    logp = os.path.join(tmp, "audio.csv")
    rnd = random.Random(1)
    with open(logp, "w") as f:
        f.write("host_time,sample_time,frames,now,peak\n")
        cum = 0
        while cum < n:
            fr = min(rnd.choice([480, 512, 1024]), n - cum)
            f.write(f"{T0 + cum / rate:.9f},{cum},{fr},{T0 + cum / rate:.6f},0.5\n")
            cum += fr
    beeps, info = analyze.beep_onsets(wavp, logp)
    check("beep count", len(beeps) == dur - 2, f"{len(beeps)}")
    err = max(abs(b - (T0 + k + off)) for b, k in zip(beeps, range(1, dur - 1)))
    check("beep onset within 0.3 ms", err < 3e-4, f"max err {err * 1e3:.3f} ms")
    # frame log at 60 fps, flash (4 capture frames of luma 235) at integer seconds
    flp = os.path.join(tmp, "frames.csv")
    with open(flp, "w") as f:
        f.write("seq,pts,display_time,arrival,w,h,stride0,cx,cy,cw,ch,content_scale,scale_factor,luma\n")
        for i in range(60 * dur):
            t = T0 + i / 60
            fm = i % 60
            luma = 235 if fm < 4 else 45 + rnd.random() * 5
            f.write(f"{i + 1},{t:.9f},{t:.9f},{t + .005:.9f},1920,1080,1920,0,0,1920,1080,1,1,{luma:.2f}\n")
    fl = analyze.flash_onsets(flp)
    check("flash count", len(fl) == dur - 1, f"{len(fl)}")
    check("flash prev-dark precedes", all(p < t for t, p in fl))
    pairs = analyze.pair_offsets(fl, beeps)
    offs = [o for _, o in pairs]
    st = analyze.stats([o * 1e3 for o in offs], [t for t, _ in pairs])
    check("offset recovered within 1 ms", abs(st["median"] - off * 1e3) < 1.0 and st["p2p"] < 1.0,
          f"median {st['median']:.3f} ms p2p {st['p2p']:.3f}")
    # goertzel
    x = [0.5 * math.sin(2 * math.pi * 430 * i / rate) for i in range(rate)]
    check("goertzel own -6 dB", abs(analyze.goertzel_db(x, rate, 430) - (-6.02)) < 0.1)
    check("goertzel other < -60 dB", analyze.goertzel_db(x, rate, 570) < -60)
    # slope
    ts = list(range(100))
    st = analyze.stats([0.001 * t / 60 * 60 for t in ts], ts)  # 1 ms per s = 60 ms/min
    check("slope per minute", abs(st["slope_per_min"] - 0.06) < 1e-6, f"{st['slope_per_min']}")


def test_media():
    f = os.path.join(MEDIA_DIR, "test_10s.mp4")
    if not os.path.exists(f):
        print("SKIP media (not generated)")
        return
    ok = True
    for n in (0, 1, 29, 30, 31, 123, 299):
        raw = subprocess.run(["ffmpeg", "-v", "error", "-i", f, "-vf", f"select=eq(n\\,{n})", "-frames:v", "1",
                              "-pix_fmt", "gray", "-f", "rawvideo", "-"], capture_output=True).stdout
        ok &= analyze.decode_barcode(raw, 1920, 1080) == n
    check("barcode decode matches frame index", ok)
    with tempfile.TemporaryDirectory() as tmp:
        w = os.path.join(tmp, "a.wav")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", f, "-vn", "-ar", "48000", "-c:a", "pcm_s16le", w])
        rate, ch, x = analyze.read_wav(w)
        t, v = analyze.envelope(x, rate, 1.0)
        ons = analyze.onsets(t, v, 0.25, 0.5)
        # AAC priming delays audio by ~21 ms; ffmpeg compensates via edit list, accept < 5 ms
        err = max(abs(o - round(o)) for o in ons) if ons else 9
        check("beeps at integer seconds", len(ons) == 10 and err < 0.005, f"n={len(ons)} max err {err * 1e3:.2f} ms")
        dur = [sum(1 for tt, vv in zip(t, v) if vv > 0.25 and k <= tt < k + 1) for k in range(1, 9)]
        check("beep length ~66.7 ms", all(60 <= d <= 75 for d in dur), f"{dur}")


def test_server():
    srv, base = server.start()
    try:
        url = base + "/test_10s.mp4"
        size = os.path.getsize(os.path.join(MEDIA_DIR, "test_10s.mp4"))
        req = urllib.request.Request(url, headers={"Range": "bytes=10-19"})
        r = urllib.request.urlopen(req)
        body = r.read()
        check("Range 206", r.status == 206 and len(body) == 10 and r.headers["Content-Range"] == f"bytes 10-19/{size}")
        with open(os.path.join(MEDIA_DIR, "test_10s.mp4"), "rb") as f:
            f.seek(10)
            check("Range bytes correct", body == f.read(10))
        r = urllib.request.urlopen(urllib.request.Request(url, headers={"Range": "bytes=-5"}))
        check("suffix range", r.status == 206 and len(r.read()) == 5)
        r = urllib.request.urlopen(base + "/player.html")
        check("html mime", r.headers["Content-Type"].startswith("text/html"))
        r = urllib.request.urlopen(base + "/setcookie?name=a&value=b&max_age=3600")
        check("Set-Cookie", "a=b" in r.headers["Set-Cookie"] and "Max-Age=3600" in r.headers["Set-Cookie"])
        urllib.request.urlopen(urllib.request.Request(base + "/whoami", headers={"Cookie": "a=b"})).read()
        check("whoami remembered", srv.last_cookies.get("/whoami") == "a=b")
    finally:
        server.stop(srv)


def test_ring(tmp):
    p = os.path.join(tmp, "ring.bin")
    if os.path.exists(helper.HELPER):
        h = helper.Helper(["shm-selftest", "--shm", p, "--size", "640x360", "--fps", "60", "--duration", "3"], "st")
        r = shmring.RingReader(p, wait=5)
        last, bad = 0, 0
        t0 = time.time()
        while time.time() - t0 < 3.5:
            fr, last = r.iter_new(last)
            bad += sum(1 for f in fr if f["data"][0] != f["seq"] % 256)
            time.sleep(1 / 60)
        h.stop()
        check("ring vs helper shm-selftest", r.frames_read > 100 and bad == 0,
              f"read {r.frames_read} bad {bad} torn {r.torn} lost {r.lost}")
    else:
        w = shmring.PyRingWriter(p, 320, 180)
        r = shmring.RingReader(p)
        stop = []
        th = threading.Thread(target=lambda: [(w.write(time.monotonic()), time.sleep(1 / 120)) for _ in range(300)])
        th.start()
        last, bad = 0, 0
        while th.is_alive():
            fr, last = r.iter_new(last)
            bad += sum(1 for f in fr if f["data"][0] != f["seq"] % 256)
            time.sleep(1 / 60)
        check("ring vs python writer", r.frames_read > 100 and bad == 0 and r.frames_read + r.lost >= 290,
              f"read {r.frames_read} bad {bad} torn {r.torn} lost {r.lost}")


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        test_analysis(tmp)
        test_media()
        test_server()
        test_ring(tmp)
    print("ALL PASS" if not fails else f"FAILED: {fails}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
