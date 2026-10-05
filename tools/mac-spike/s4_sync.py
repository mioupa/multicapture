#!/usr/bin/env python3
"""S4: do video and audio share one time base?  One Chrome plays test_5min.mp4 (flash + beep at
every integer second) with sound; one helper captures its window (video, frame-log) and taps its
audio (WAV + per-callback log).  Offsets = beep onset (audio host_time) - flash onset (video pts);
positive = audio late.

PASS iff |median offset| <= 40 ms AND peak-to-peak after constant correction <= 40 ms.
Note: flash timing is quantised to the capture frame interval (16.7 ms at 60 fps), so a few ms of
spread is expected even for a perfect pipeline."""
import argparse
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from spikelib import runner, analyze, machclock, helper as hl, chrome as chromelib  # noqa: E402


def clock_check(samples=8):
    rows = []
    for _ in range(samples):
        m0, c0, p0, mo0 = machclock.now(), machclock.continuous(), time.perf_counter(), time.monotonic()
        ev = hl.run_once(["clock"], 10)
        m1 = machclock.now()
        e = next((x for x in ev if x.get("ev") == "clock"), None)
        if not e:
            continue
        rows.append({"bracket_ms": (m1 - m0) * 1e3,
                     "py_mach_minus_helper_mach_abs_ms": ((m0 + m1) / 2 - e["mach_abs"]) * 1e3,
                     "perf_counter_minus_py_mach_ms": (p0 - m0) * 1e3,
                     "monotonic_minus_py_mach_ms": (mo0 - m0) * 1e3,
                     "py_mach_cont_minus_py_mach_ms": (c0 - m0) * 1e3,
                     "helper_cm_host_minus_mach_abs_ms": (e["cm_host"] - e["mach_abs"]) * 1e3,
                     "helper_mach_cont_minus_mach_abs_ms": (e["mach_cont"] - e["mach_abs"]) * 1e3,
                     "helper_uptime_minus_mach_abs_ms": (e["uptime"] - e["mach_abs"]) * 1e3})
        time.sleep(0.1)
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, default=120)
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--n", type=int, default=1, help="total browsers; n-1 background browsers add load (muted, video capture)")
    ap.add_argument("--audio-mode", choices=["tree", "audioservice", "main"], default="tree")
    ap.add_argument("--mute", choices=["unmuted", "muted", "mutedWhenTapped"], default="muted")
    ap.add_argument("--size", default="1920x1080")
    ap.add_argument("--skip", type=float, default=5.0, help="seconds of flashes/beeps to ignore at the start")
    ap.add_argument("--skip-perm-check", action="store_true")
    a = ap.parse_args()
    cw, ch = runner.parse_size(a.size)
    run = runner.Run("S4", a.audio_mode, a)
    run.preflight(need_screen=True, need_audio=True, skip=a.skip_perm_check)
    insts, err, main_h = [], None, None
    paths = {k: os.path.join(run.dir, v) for k, v in
             dict(frames="frames.csv", wav="tap.wav", alog="audio.csv").items()}
    try:
        dis = ["AudioServiceOutOfProcess"] if a.audio_mode == "main" else []
        for i in range(a.n):
            muted = 0 if i == 0 else 1
            url = run.player_url(src="/test_5min.mp4", muted=muted, loop=1, label=f"mc-s4-{i}")
            insts.append(run.launch_chrome(f"w{i}", url, 100 + 36 * i, 100 + 36 * i, cw, ch, disable_features=dis))
        for inst in insts:
            run.log(f"{inst.name}: playing={inst.wait_playing(30)}")
        wids = [chromelib.find_window_id(inst.pid, f"mc-s4-{i}") for i, inst in enumerate(insts)]
        run.log(f"window ids {wids}")
        if None in wids:
            raise RuntimeError("window id not found")
        top = insts[0].content_inset()[1]
        run.log("clock check ...")
        clk = clock_check()
        run.summary["clock_check"] = clk
        for i in range(1, a.n):  # background load first
            run.start_helper(runner.video_args(wids[i], (cw, ch), (0, top, cw, ch), 30,
                                               os.path.join(run.dir, f"frames-bg{i}.csv")), f"bg{i}")
            runner.sleep_interruptible(0.5)
        args = runner.video_args(wids[0], (cw, ch), (0, top, cw, ch), a.fps, paths["frames"])
        args += ["--audio-out", paths["wav"], "--audio-log", paths["alog"], "--mute", a.mute,
                 "--duration", a.seconds + 5]
        if a.audio_mode == "tree":
            args += ["--audio-tree", insts[0].pid]
        elif a.audio_mode == "audioservice":
            asp = insts[0].audio_service_pid()
            if not asp:
                raise RuntimeError("audio service pid not found")
            args += ["--audio-pid", asp]
        else:
            args += ["--audio-pid", insts[0].pid]
        main_h = run.start_helper(args, "main")
        for ev in ("first_frame", "started"):
            e = main_h.wait_for(ev, 15)
            run.log(f"{ev}: {e}")
        runner.sleep_interruptible(a.seconds)
    except KeyboardInterrupt:
        err = "interrupted"
    except Exception as e:
        err = repr(e)
        run.log("ERROR " + err)
    finally:
        run.stop_helpers()
        run.cleanup()

    lines = [f"S4 audio-mode={a.audio_mode} mute={a.mute} fps={a.fps} n={a.n} seconds={a.seconds}"]
    passed = False
    try:
        beeps, binfo = analyze.beep_onsets(paths["wav"], paths["alog"])
        rows = analyze.read_frame_log(paths["frames"])
        t0 = rows[0]["pts"] if rows else 0
        res = {}
        for col in ("pts", "display_time"):
            fl = [f for f in analyze.flash_onsets(rows, col) if f[0] - t0 >= a.skip]
            pr = analyze.pair_offsets(fl, beeps)
            off_ms = [o * 1e3 for _, o in pr]
            ts = [t for t, _ in pr]
            st = analyze.stats(off_ms, ts)
            if st["n"] >= 3:
                slope = st.get("slope_per_min", 0) / 1.0
                mt, mv = statistics.fmean(ts), st["mean"]
                resid = [v - (mv + slope / 60 * (t - mt)) for v, t in zip(off_ms, ts)]
                st["p2p_after_drift_removed"] = max(resid) - min(resid)
            res[col] = {"flashes": len(fl), "beeps": len(beeps), "stats_ms": st}
        run.summary["offsets"] = res
        st = res["pts"]["stats_ms"]
        if st["n"]:
            lines.append(f"flashes={res['pts']['flashes']} beeps={len(beeps)} pairs={st['n']}  (offset = beep - flash, +ve = audio late)")
            lines.append("offset ms (video pts): mean %.2f median %.2f stdev %.2f min %.2f max %.2f p2p %.2f"
                         % (st["mean"], st["median"], st["stdev"], st["min"], st["max"], st["p2p"]))
            lines.append("  drift %.3f ms/min; p2p after constant correction %.2f ms; after drift removed %s ms"
                         % (st.get("slope_per_min", 0), st["p2p"], "%.2f" % st["p2p_after_drift_removed"] if "p2p_after_drift_removed" in st else "n/a"))
            sd = res["display_time"]["stats_ms"]
            if sd["n"]:
                lines.append("offset ms (display_time): median %.2f p2p %.2f" % (sd["median"], sd["p2p"]))
            passed = abs(st["median"]) <= 40 and st["p2p"] <= 40 and err is None
        else:
            lines.append("no flash/beep pairs found (beeps=%d, flashes=%d); audio info %s" % (len(beeps), res["pts"]["flashes"], binfo))
        lat_v = [(r["arrival"] - r["pts"]) * 1e3 for r in rows]
        arows = analyze.read_audio_log(paths["alog"])
        lat_a = [(r["now"] - r["host_time"]) * 1e3 for r in arows]
        sv, sa = analyze.stats(lat_v), analyze.stats(lat_a)
        run.summary["latency_ms"] = {"video_arrival_minus_pts": sv, "audio_now_minus_host_time": sa}
        lines.append("latency video arrival-pts ms: median %.1f min %.1f max %.1f | audio now-host_time ms: median %.2f min %.2f max %.2f"
                     % (sv["median"], sv["min"], sv["max"], sa["median"], sa["min"], sa["max"]))
        ff = main_h.find("first_frame") if main_h else []
        if ff:
            lines.append(f"first_frame: {ff[0].get('w')}x{ff[0].get('h')} scale_factor={ff[0].get('scale_factor')}")
    except Exception as e:
        lines.append(f"analysis failed: {e!r}")
    clk = run.summary.get("clock_check") or []
    if clk:
        best = min(clk, key=lambda r: r["bracket_ms"])
        lines.append("clock check (best of %d, bracket %.1f ms; ms):" % (len(clk), best["bracket_ms"]))
        for k, v in best.items():
            if k != "bracket_ms":
                lines.append(f"  {k}: {v:.3f}")
    if err:
        lines.append("run ended early: " + err)
    lines.append("criteria: |median| <= 40 ms and p2p after constant correction <= 40 ms")
    run.finish(lines, passed)


if __name__ == "__main__":
    main()
