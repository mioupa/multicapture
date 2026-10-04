#!/usr/bin/env python3
"""Fake mc-spike for exercising the runners end-to-end without TCC / Swift:
   MC_SPIKE_HELPER=tools/mac-spike/fake_helper.py python3 s1_occlusion.py --seconds 20 --skip-perm-check
Synthesises flashes (luma), beeps (20 ms after each flash), stats events, a shm ring and PNG dumps."""
import json
import os
import select
import struct
import subprocess
import sys
import time
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from spikelib import machclock, shmring, MEDIA_DIR  # noqa: E402


def emit(label, **kw):
    if label:
        kw["label"] = label
    print(json.dumps(kw), flush=True)


def opt(argv, name, default=None, multi=False):
    vals = [argv[i + 1] for i, v in enumerate(argv) if v == name and i + 1 < len(argv)]
    if multi:
        return vals
    return vals[-1] if vals else default


def main():
    argv = sys.argv[1:]
    cmd = argv[0] if argv else ""
    label = opt(argv, "--label")
    if cmd == "perm":
        emit(label, ev="perm", screen=True, audio="granted")
    elif cmd == "clock":
        m = machclock.now()
        emit(label, ev="clock", mach_abs=m, cm_host=m, mach_cont=machclock.continuous(), uptime=m)
    elif cmd == "windows":
        for p in opt(argv, "--pid", multi=True):
            emit(label, ev="window", window_id=int(p), pid=int(p), bundle_id="fake", app="Chrome", title="",
                 frame=[0, 0, 100, 100], on_screen=True, layer=0, active=False)
        emit(label, ev="done", count=1)
    elif cmd == "audio-procs":
        emit(label, ev="done", count=0)
    elif cmd == "capture":
        capture(argv, label)


def capture(argv, label):
    wid = opt(argv, "--window-id")
    size = opt(argv, "--size", "1920x1080")
    w, h = map(int, size.split("x"))
    fps = float(opt(argv, "--fps", 30))
    duration = float(opt(argv, "--duration", 0))
    flog = opt(argv, "--frame-log")
    audio = bool(opt(argv, "--audio-pid") or opt(argv, "--audio-tree"))
    wav_path, alog_path = opt(argv, "--audio-out"), opt(argv, "--audio-log")
    shm = opt(argv, "--shm")
    dump_dir, dump_seqs = opt(argv, "--dump-dir"), [int(x) for x in (opt(argv, "--dump-seqs", "") or "").split(",") if x]
    t0 = machclock.now()
    ring = shmring.PyRingWriter(shm, w, h) if shm and wid else None
    ff = open(flog, "w") if flog and wid else None
    if ff:
        ff.write("seq,pts,display_time,arrival,w,h,stride0,cx,cy,cw,ch,content_scale,scale_factor,luma\n")
    wf = al = None
    if audio:
        wf = wave.open(wav_path, "wb") if wav_path else None
        if wf:
            wf.setnchannels(2); wf.setsampwidth(2); wf.setframerate(48000)
        al = open(alog_path, "w") if alog_path else None
        if al:
            al.write("host_time,sample_time,frames,now,peak\n")
        emit(label, ev="started", pids=[1], object_ids=[1], sample_rate=48000, channels=2, device="fake")
    vseq, acb = 0, 0
    nv, na = t0, t0
    nstat, comp, acount = t0 + 1, 0, 0
    stdin_open = True
    while True:
        now = machclock.now()
        if duration and now - t0 >= duration:
            break
        if select.select([sys.stdin], [], [], 0)[0] and not sys.stdin.readline():
            break
        did = False
        if wid and now >= nv:
            vseq += 1
            if vseq == 1:
                emit(label, ev="first_frame", w=w, h=h, stride0=w, content_rect=[0, 0, w, h], content_scale=1.0,
                     scale_factor=1.0, matrix="709", primaries="709", transfer="709")
            ph = nv % 1.0
            luma = 235 if ph < 2 / 30 else 45
            if ff:
                ff.write(f"{vseq},{nv:.9f},{nv + 0.01:.9f},{now:.9f},{w},{h},{w},0,0,{w},{h},1,1,{luma}\n")
            if ring:
                ring.write(nv)
            if vseq in dump_seqs and dump_dir:
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", os.path.join(MEDIA_DIR, "test_10s.mp4"), "-vf",
                                f"select=eq(n\\,{vseq}),scale={w}:{h}", "-frames:v", "1", os.path.join(dump_dir, f"{vseq}.png")])
            comp += 1
            nv += 1 / fps
            did = True
        if audio and now >= na:
            frames = 512
            ht = na
            pcm = bytearray()
            peak = 0.0
            for i in range(frames):
                hh = ht + i / 48000
                ph = hh % 1.0
                v = 0.5 * __import__("math").sin(2 * 3.141592653589793 * 1000 * hh) if 0.02 <= ph < 0.02 + 2 / 30 else 0.0
                peak = max(peak, abs(v))
                s = int(v * 32767)
                pcm += struct.pack("<hh", s, s)
            if wf:
                wf.writeframes(bytes(pcm))
            if al:
                al.write(f"{ht:.9f},{acb * frames},{frames},{ht + 0.001:.9f},{peak:.3f}\n")
            acb += 1
            acount += 1
            na += frames / 48000
            did = True
        if now >= nstat:
            emit(label, ev="stats", t=now, complete=comp, idle=0, blank=0, suspended=0, started=0, stopped=0,
                 audio_cb=acount, audio_frames=acount * 512, audio_zero_cb=0, audio_peak=0.5)
            comp = acount = 0
            nstat += 1
        if not did:
            time.sleep(0.001)
    emit(label, ev="stopped", totals={"frames": vseq})
    if ff: ff.close()
    if al: al.close()
    if wf: wf.close()


if __name__ == "__main__":
    main()
