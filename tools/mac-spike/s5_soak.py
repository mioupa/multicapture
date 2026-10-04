#!/usr/bin/env python3
"""S5: 8 streams + 8 audio taps, startup behaviour and long-run stability.

Part A (startup): for each value of --stagger-list, --startup-cycles cycles of starting N helpers
  (video+audio) against N Chromes that stay up; measures time to first_frame / audio started,
  counts failures (nothing within 15 s, or an error event), then stops all helpers.
Part B (soak): --minutes of N helpers (nv12 1920x1080 30 fps video + --shm ring + muted audio tap)
  while a Python thread per ring reads with shmring.iter_new (~60 Hz). Samples ps (helpers, Chrome
  trees, replayd) every 30 s.
  PASS iff no error / stall / audio_silent events and every 1 s stats bin >= 29 complete frames
  (after the first 5 s).
--part A|B|both selects the parts (default both). --seconds overrides --minutes."""
import argparse
import os
import statistics
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from spikelib import runner, analyze, shmring, chrome as chromelib, helper as hl  # noqa: E402
from spikelib.runner import fmt  # noqa: E402


def helper_args(wid, size, top, fps, shm=None, mute="muted", pid=None):
    cw, ch = size
    args = ["capture", "--window-id", wid, "--size", f"{cw}x{ch}", "--fps", fps, "--crop", f"0,{top},{cw},{ch}",
            "--pixfmt", "nv12", "--audio-tree", pid, "--mute", mute]
    if shm:
        args += ["--shm", shm]
    return args


def ring_reader(path, stop, out):
    try:
        r = shmring.RingReader(path, wait=20)
    except Exception as e:
        out["error"] = repr(e)
        return
    last, bad = 0, 0
    while not stop.is_set():
        try:
            frames, last = r.iter_new(last)
            for f in frames:
                if len(f["data"]) != f["w"] * f["h"] * 3 // 2:
                    bad += 1
        except Exception as e:
            out["error"] = repr(e)
            break
        stop.wait(1 / 60)
    out.update({"frames_read": r.frames_read, "torn": r.torn, "torn_failed": r.torn_failed, "lost": r.lost,
                "short_frames": bad, "last_seq": last, "latest_seq": r.latest_seq()})
    r.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--part", choices=["A", "B", "both"], default="both")
    ap.add_argument("--n", type=int, default=8)
    ap.add_argument("--startup-cycles", type=int, default=5)
    ap.add_argument("--stagger-list", default="0,0.5")
    ap.add_argument("--minutes", type=float, default=60)
    ap.add_argument("--seconds", type=float, help="overrides --minutes (short run)")
    ap.add_argument("--stagger", type=float, default=0.5)
    ap.add_argument("--size", default="1920x1080")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--skip-perm-check", action="store_true")
    a = ap.parse_args()
    cw, ch = runner.parse_size(a.size)
    duration = a.seconds if a.seconds else a.minutes * 60
    run = runner.Run("S5", a.part, a)
    run.preflight(need_screen=True, need_audio=True, skip=a.skip_perm_check)
    insts, err = [], None
    partA, partB, lines = {}, {}, []
    passed = True
    try:
        for i in range(a.n):
            url = run.player_url(src="/test_5min.mp4", muted=0, loop=1, label=f"mc-s5-{i}")
            insts.append(run.launch_chrome(f"w{i}", url, 100 + 36 * i, 100 + 36 * i, cw, ch))
        for inst in insts:
            run.log(f"{inst.name}: playing={inst.wait_playing(30)}")
        wids = [chromelib.find_window_id(inst.pid, f"mc-s5-{i}") for i, inst in enumerate(insts)]
        run.log(f"window ids {wids}")
        if None in wids:
            raise RuntimeError("window id not found")
        top = insts[0].content_inset()[1]

        # ------------------------------------------------------------ Part A
        if a.part in ("A", "both"):
            for stg in [float(x) for x in a.stagger_list.split(",")]:
                tf, ta, fails, errors = [], [], 0, 0
                for k in range(a.startup_cycles):
                    hs = []
                    for i in range(a.n):
                        hs.append(run.start_helper(helper_args(wids[i], (cw, ch), top, a.fps, pid=insts[i].pid),
                                                   f"A{stg}-c{k}-w{i}"))
                        runner.sleep_interruptible(stg)
                    for h in hs:
                        left = lambda: max(0.1, a.timeout - (time.monotonic() - h.t_start))
                        ff = h.wait_for("first_frame", left())
                        st = h.wait_for("started", left())
                        if ff:
                            tf.append(ff["_recv"] - h.t_start)
                        if st:
                            ta.append(st["_recv"] - h.t_start)
                        if not ff or not st:
                            fails += 1
                        errors += len(h.find("error"))
                    run.log(f"stagger {stg} cycle {k}: first_frame ok {sum(1 for h in hs if h.find('first_frame'))}/{a.n}, "
                            f"audio ok {sum(1 for h in hs if h.find('started'))}/{a.n}")
                    run.stop_helpers(hs)
                    for h in hs:
                        run.helpers.remove(h)
                    runner.sleep_interruptible(2.0)
                partA[stg] = {"cycles": a.startup_cycles, "helpers": a.startup_cycles * a.n, "fail_helpers": fails,
                              "errors": errors, "first_frame_s": analyze.stats(tf), "audio_started_s": analyze.stats(ta)}
                passed &= (fails == 0 and errors == 0)

        # ------------------------------------------------------------ Part B
        if a.part in ("B", "both"):
            stop = threading.Event()
            ringout, threads, helpers, samples = {}, [], [], []
            try:
                os.makedirs(os.path.join(run.dir, "rings"), exist_ok=True)
                for i in range(a.n):
                    ring = os.path.join(run.dir, "rings", f"w{i}.ring")
                    ringout[i] = {}
                    t = threading.Thread(target=ring_reader, args=(ring, stop, ringout[i]), daemon=True)
                    t.start()
                    threads.append(t)
                    helpers.append(run.start_helper(helper_args(wids[i], (cw, ch), top, a.fps, shm=ring,
                                                                pid=insts[i].pid), f"B-w{i}"))
                    runner.sleep_interruptible(a.stagger)
                for h in helpers:
                    run.log(f"{h.label}: first_frame={bool(h.wait_for('first_frame', 15))} audio_started={bool(h.wait_for('started', 15))}")
                t_start = time.monotonic()
                next_s = t_start
                while time.monotonic() - t_start < duration:
                    if time.monotonic() >= next_s:
                        next_s += 30
                        tree = {i: [inst.pid] + [d["pid"] for d in inst.descendants()] for i, inst in enumerate(insts)}
                        rpids = runner.pids_by_name("replayd")
                        ps = runner.ps_sample([h.proc.pid for h in helpers] + [p for l in tree.values() for p in l] + rpids)
                        samples.append({
                            "t": time.monotonic() - t_start,
                            "helpers": {h.label: ps.get(h.proc.pid) for h in helpers},
                            "chrome_tree": {i: [sum(ps.get(p, (0, 0))[0] for p in l), sum(ps.get(p, (0, 0))[1] for p in l)]
                                            for i, l in tree.items()},
                            "replayd": [ps[p] for p in rpids if p in ps]})
                        run.log("t=%.0fs helpers alive %d/%d  events: stall=%d error=%d silent=%d" % (
                            samples[-1]["t"], sum(1 for h in helpers if h.alive()), len(helpers),
                            sum(len(h.find("stall")) for h in helpers), sum(len(h.find("error")) for h in helpers),
                            sum(len(h.find("audio_silent")) for h in helpers)))
                    time.sleep(0.5)
            finally:
                run.stop_helpers(helpers)
                stop.set()
                for t in threads:
                    t.join(5)
            partB = {"samples": samples, "rings": ringout}
            lb, tot_bad, per = [], 0, {}
            lb.append(f"B: {a.n} helpers nv12 {cw}x{ch}@{a.fps} + shm ring + muted tap, {duration:.0f}s")
            lb.append("hlp  frames <29 <28 minbin stall err silent adev zero_cb/cb | ring read torn lost")
            for i, h in enumerate(helpers):
                allst = h.find("stats")
                comp = [e.get("complete", 0) for e in allst][5:]
                low, low28 = sum(1 for c in comp if c < 29), sum(1 for c in comp if c < 28)
                tot = sum(e.get("complete", 0) for e in allst)
                zero, cb = sum(e.get("audio_zero_cb", 0) for e in allst), sum(e.get("audio_cb", 0) for e in allst)
                ev = {k: len(h.find(k)) for k in ("stall", "error", "audio_silent", "audio_device_changed")}
                ro = ringout.get(i, {})
                ok = low == 0 and ev["stall"] == 0 and ev["error"] == 0 and ev["audio_silent"] == 0 and "error" not in ro and bool(comp)
                tot_bad += (not ok)
                per[i] = {"frames": tot, "low29": low, "low28": low28, "min_bin": min(comp) if comp else None,
                          "events": ev, "audio_zero_cb": zero, "audio_cb": cb, "ring": ro, "pass": ok}
                lb.append(f"{i:>3d} {tot:>7d} {low:>3d} {low28:>3d} {fmt(min(comp) if comp else None, 0):>6} {ev['stall']:>5d} {ev['error']:>3d} "
                          f"{ev['audio_silent']:>6d} {ev['audio_device_changed']:>4d} {zero}/{cb} | {ro.get('frames_read')} {ro.get('torn')} {ro.get('lost')}"
                          f"{'' if ok else '  <-- FAIL'}")
            if samples:
                rng = lambda vals: f"{min(vals):.0f}..{max(vals):.0f}" if vals else "n/a"
                hc = [v[0] for s in samples for v in s["helpers"].values() if v]
                hr = [v[1] / 1024 for s in samples for v in s["helpers"].values() if v]
                cc = [v[0] for s in samples for v in s["chrome_tree"].values()]
                cr = [v[1] / 1024 for s in samples for v in s["chrome_tree"].values()]
                rc = [v[0] for s in samples for v in s["replayd"]]
                rr = [v[1] / 1024 for s in samples for v in s["replayd"]]
                lb.append(f"CPU% helper {rng(hc)} | chrome tree {rng(cc)} | replayd {rng(rc)}")
                lb.append(f"RSS MB helper {rng(hr)} | chrome tree {rng(cr)} | replayd {rng(rr)}")
            ff = [e for h in helpers for e in h.find("first_frame")]
            if ff:
                lb.append(f"first_frame: {ff[0].get('w')}x{ff[0].get('h')} scale_factor={ff[0].get('scale_factor')}")
            partB["per_helper"] = per
            passed &= tot_bad == 0 and bool(helpers)
            lines += lb
    except KeyboardInterrupt:
        err = "interrupted"
        passed = False
    except Exception as e:
        err = repr(e)
        passed = False
        run.log("ERROR " + err)
    finally:
        run.cleanup()

    head = []
    if partA:
        head.append(f"A: startup, {a.n} helpers/cycle, timeout {a.timeout:.0f}s")
        for stg, r in partA.items():
            f, s = r["first_frame_s"], r["audio_started_s"]
            head.append(f"  stagger {stg}: {r['cycles']} cycles, fail {r['fail_helpers']}/{r['helpers']} helpers, errors {r['errors']}; "
                        f"first_frame mean/max {fmt(f.get('mean'), 2)}/{fmt(f.get('max'), 2)} s; audio started mean/max {fmt(s.get('mean'), 2)}/{fmt(s.get('max'), 2)} s")
    lines = head + lines
    if err:
        lines.append("run ended early: " + err)
    lines.append("criteria: no error/stall/audio_silent; every 1 s stats bin >= 29 complete frames after first 5 s; startup: no failures")
    run.summary.update({"partA": partA, "partB": partB})
    run.finish(lines, passed and not err)


if __name__ == "__main__":
    main()
