#!/usr/bin/env python3
"""S1: do fully hidden Chrome windows keep rendering?  N stacked Chrome windows play a looped
video; one helper per window captures video only (SCK complete frames) while the page reports rAF
and requestVideoFrameCallback rates over CDP.

PASS iff every window has: min 10 s complete fps >= 29 AND min rVFC/s >= 29 AND min rAF/s >= 29
(first 5 s excluded; rAF/rVFC rates come from the 5 s CDP polls)."""
import argparse
import os
import shlex
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from spikelib import runner, analyze, chrome as chromelib  # noqa: E402
from spikelib.runner import fmt  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=8)
    ap.add_argument("--minutes", type=float, default=10)
    ap.add_argument("--seconds", type=float, help="overrides --minutes (short run)")
    ap.add_argument("--layout", choices=["stack", "cascade"], default="stack")
    ap.add_argument("--cover", action="store_true", help="extra dark Chrome window on top of all (not measured)")
    ap.add_argument("--extra-flags", default="", help='extra Chrome flags, e.g. "--foo --bar=1"')
    ap.add_argument("--disable-features", default="")
    ap.add_argument("--enable-features", default="")
    ap.add_argument("--variant", default="", help="name used in the results dir")
    ap.add_argument("--size", default="1920x1080", help="content size WxH")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--stagger", type=float, default=0.5)
    ap.add_argument("--poll", type=float, default=5.0)
    ap.add_argument("--video", default="test_5min.mp4")
    ap.add_argument("--skip-perm-check", action="store_true")
    ap.add_argument("--cover-size", default="3300x2300", help="cover window size WxH at (0,0)")
    ap.add_argument("--sound", action="store_true",
                    help="play with sound; each helper also taps its browser with mute=muted so the room stays quiet")
    a = ap.parse_args()
    cw, ch = runner.parse_size(a.size)
    duration = a.seconds if a.seconds else a.minutes * 60
    run = runner.Run("S1", a.variant or None, a)
    run.preflight(need_screen=True, skip=a.skip_perm_check)
    insts, helpers, polls, logs, wids = [], [], {}, {}, {}
    err = None
    try:
        run.log(f"S1 n={a.n} layout={a.layout} cover={a.cover} variant={a.variant} duration={duration:.0f}s")
        for i in range(a.n):
            off = 0 if a.layout == "stack" else 36 * i
            url = run.player_url(src="/" + a.video, muted=0 if a.sound else 1, loop=1, label=f"mc-s1-{i}")
            inst = run.launch_chrome(f"w{i}", url, 100 + off, 100 + off, cw, ch, shlex.split(a.extra_flags),
                                     a.disable_features, a.enable_features)
            insts.append(inst)
        for inst in insts:
            ok = inst.wait_playing(30)
            run.log(f"{inst.name}: playing={ok} inner/outer={inst._sizes()}")
        for i, inst in enumerate(insts):
            wids[i] = chromelib.find_window_id(inst.pid, f"mc-s1-{i}")
            run.log(f"{inst.name}: pid={inst.pid} window_id={wids[i]} audio_svc={inst.audio_service_pid()}")
        if any(w is None for w in wids.values()):
            raise RuntimeError(f"window ids not found: {wids}")
        run.summary["window_ids"] = wids
        if a.cover:
            cvw, cvh = runner.parse_size(a.cover_size)
            cover = run.launch_chrome("cover", run.base + "/cover.html", 0, 0, cvw, cvh, set_size=False)
            time.sleep(1.0)
            run.log(f"cover window sizes {cover._sizes()}")
        top = insts[0].content_inset()[1]
        run.summary["content_inset_top"] = top
        for i, inst in enumerate(insts):
            logs[i] = os.path.join(run.dir, f"frames-w{i}.csv")
            hargs = runner.video_args(wids[i], (cw, ch), (0, top, cw, ch), a.fps, logs[i])
            if a.sound:
                hargs += ["--audio-tree", str(inst.pid), "--mute", "muted"]
            h = run.start_helper(hargs, f"w{i}")
            helpers.append(h)
            runner.sleep_interruptible(a.stagger)
        for h in helpers:
            if h.wait_for("first_frame", 15) is None:
                run.log(f"{h.label}: no first_frame within 15 s")
        t_start = time.monotonic()
        polls = {i: [] for i in range(a.n)}
        next_poll = t_start
        while time.monotonic() - t_start < duration:
            if time.monotonic() >= next_poll:
                for i, inst in enumerate(insts):
                    try:
                        c = inst.counters()
                        if c:
                            polls[i].append((time.monotonic(), c))
                    except Exception as e:
                        run.log(f"{inst.name}: poll failed {e}")
                next_poll += a.poll
                el = time.monotonic() - t_start
                if int(el) % 30 < a.poll:
                    run.log("progress %.0fs  " % el + " ".join(f"w{i}:{polls[i][-1][1]['vfc'] if polls[i] else '-'}" for i in range(a.n)))
            time.sleep(0.2)
    except KeyboardInterrupt:
        err = "interrupted"
        run.log("interrupted; summarising what we have")
    except Exception as e:
        err = repr(e)
        run.log(f"ERROR {err}")
    finally:
        run.stop_helpers()
        final = {}
        for i, inst in enumerate(insts):
            try:
                final[i] = inst.counters()
            except Exception:
                final[i] = None
        for inst in insts:
            pass
        run.cleanup()

    # ---- analysis
    lines, allpass, per = [], bool(insts) and err is None, {}
    lines.append(f"S1 n={a.n} layout={a.layout} cover={a.cover} variant={a.variant or '-'} {duration:.0f}s "
                 f"flags=[{a.extra_flags}] dis=[{a.disable_features}] en=[{a.enable_features}]")
    lines.append("win  sck_mean sck_min10 %bins<29 | raf_mean raf_min | vfc_mean vfc_min | dropped/total stalls")
    for i in range(len(insts)):
        rows = analyze.read_frame_log(logs[i]) if i in logs and os.path.exists(logs[i]) else []
        fs = runner.fps_stats(rows, 5.0)
        raf_m, raf_n = runner.rate_from_polls(polls.get(i, []), "raf")
        vfc_m, vfc_n = runner.rate_from_polls(polls.get(i, []), "vfc")
        f = final.get(i) or {}
        stalls = len(helpers[i].find("stall")) if i < len(helpers) else -1
        errs = len(helpers[i].find("error")) if i < len(helpers) else -1
        ok = fs["min_win"] >= 29 and (vfc_n or 0) >= 29 and (raf_n or 0) >= 29
        allpass &= ok
        per[i] = {"sck": fs, "raf_mean": raf_m, "raf_min": raf_n, "vfc_mean": vfc_m, "vfc_min": vfc_n,
                  "dropped": f.get("dropped"), "total": f.get("total"), "stalls": stalls, "helper_errors": errs,
                  "pass": ok}
        lines.append(f"w{i}   {fmt(fs['mean'])}     {fmt(fs['min_win'])}     {fmt(fs.get('pct_bins_low', 0))}    |"
                     f" {fmt(raf_m)}   {fmt(raf_n)}  | {fmt(vfc_m)}   {fmt(vfc_n)} | {f.get('dropped')}/{f.get('total')} "
                     f"{stalls}{' ERR' if errs else ''}{'' if ok else '  <-- FAIL'}")
    ff = [e for h in helpers for e in h.find("first_frame")]
    if ff:
        lines.append(f"first_frame: {ff[0].get('w')}x{ff[0].get('h')} scale_factor={ff[0].get('scale_factor')} "
                     f"content_scale={ff[0].get('content_scale')}")
    if err:
        lines.append(f"run ended early: {err}")
    lines.append("criteria: every window min10s complete fps>=29, min rVFC/s>=29, min rAF/s>=29")
    run.summary["per_window"] = per
    run.finish(lines, allpass)


if __name__ == "__main__":
    main()
