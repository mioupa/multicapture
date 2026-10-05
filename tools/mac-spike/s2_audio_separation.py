#!/usr/bin/env python3
"""S2: per-browser audio isolation. N Chromes each play a different sine tone (no video); one
helper tap per Chrome records audio. Analysis: N x N Goertzel matrix (tap i x freq j) over the
middle of the recording.

PASS iff every tap's own level > -40 dBFS and margin (own - max(other)) >= 30 dB."""
import argparse
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from spikelib import runner, analyze, chrome as chromelib  # noqa: E402

FREQS = [310, 430, 570, 690, 830, 970, 1110, 1270]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=8)
    ap.add_argument("--seconds", type=float, default=20)
    ap.add_argument("--mode", choices=["tree", "audioservice", "main"], default="tree",
                    help="tree: --audio-tree <main pid>; audioservice: --audio-pid <audio service pid>; "
                         "main: --audio-pid <main pid> + AudioServiceOutOfProcess disabled")
    ap.add_argument("--mute", choices=["unmuted", "muted", "mutedWhenTapped"], default="muted")
    ap.add_argument("--gain", type=float, default=0.2)
    ap.add_argument("--stagger", type=float, default=0.5)
    ap.add_argument("--skip-perm-check", action="store_true")
    a = ap.parse_args()
    run = runner.Run("S2", a.mode, a)
    run.preflight(need_screen=False, need_audio=True, skip=a.skip_perm_check)
    from spikelib import helper as hl
    freqs = FREQS[:a.n]
    insts, helpers, wavs, logs = [], [], {}, {}
    err = None
    try:
        dis = ["AudioServiceOutOfProcess"] if a.mode == "main" else []
        for i in range(a.n):
            url = run.player_url(anim=1, tone=freqs[i], gain=a.gain, label=f"mc-s2-{i}")
            insts.append(run.launch_chrome(f"w{i}", url, 100 + 40 * i, 100 + 40 * i, 480, 320,
                                           disable_features=dis))
        for inst in insts:
            run.log(f"{inst.name}: ready={inst.wait_playing(30)} audioState={(inst.counters() or {}).get('audioState')}")
        time.sleep(2)
        # audio process objects mapped to chrome process types
        ap_ev = hl.run_once(["audio-procs"], 20)
        ps_map = {}
        for inst in insts:
            for p in [{"pid": inst.pid, "type": "browser", "utility_sub_type": None}] + inst.descendants():
                ps_map[p["pid"]] = (inst.name, p["type"] or "browser", p.get("utility_sub_type"))
        mapped = []
        for e in ap_ev:
            if e.get("ev") != "audio_proc":
                continue
            who = ps_map.get(e["pid"])
            mapped.append({**e, "chrome": who[0] if who else None, "chrome_type": who[1] if who else None,
                           "sub": who[2] if who else None})
        json.dump(mapped, open(os.path.join(run.dir, "audio_procs_mapped.json"), "w"), indent=1)
        run.summary["audio_procs"] = [m for m in mapped if m["chrome"]]
        run.log("audio processes belonging to our Chromes: " +
                ", ".join(f"{m['pid']}={m['chrome']}/{m['chrome_type']}{'/' + m['sub'] if m['sub'] else ''}"
                          f"{'(out)' if m.get('running_output') else ''}" for m in mapped if m["chrome"]))
        for i, inst in enumerate(insts):
            wavs[i] = os.path.join(run.dir, f"tap{i}.wav")
            logs[i] = os.path.join(run.dir, f"tap{i}.csv")
            args = ["capture", "--audio-out", wavs[i], "--audio-log", logs[i], "--mute", a.mute,
                    "--duration", a.seconds + 5]
            if a.mode == "tree":
                args += ["--audio-tree", inst.pid]
            elif a.mode == "audioservice":
                asp = inst.audio_service_pid()
                run.log(f"{inst.name}: audio service pid {asp}")
                if not asp:
                    raise RuntimeError(f"{inst.name}: audio service process not found")
                args += ["--audio-pid", asp]
            else:
                args += ["--audio-pid", inst.pid]
            helpers.append(run.start_helper(args, f"w{i}"))
            runner.sleep_interruptible(a.stagger)
        for h in helpers:
            s = h.wait_for("started", 15)
            run.log(f"{h.label}: started={bool(s)} {({k: s.get(k) for k in ('pids', 'object_ids', 'sample_rate', 'channels', 'device')} if s else '')}")
        runner.sleep_interruptible(a.seconds)
    except KeyboardInterrupt:
        err = "interrupted"
    except Exception as e:
        err = repr(e)
        run.log("ERROR " + err)
    finally:
        run.stop_helpers()
        run.cleanup()

    # ---- analysis
    n = len(helpers)
    matrix, lines = [], []
    own, margin, passed = [], [], err is None and n == a.n and n > 0
    for i in range(n):
        row = []
        if os.path.exists(wavs.get(i, "")):
            try:
                rate, ch, x = analyze.read_wav(wavs[i])
            except Exception as e:
                rate, x = 48000, []
                run.log(f"tap{i}: wav unreadable {e}")
            L = len(x)
            seg = x[int(L * 0.25):int(L * 0.75)]      # middle half
            seg = seg[:int(rate * 6)]                  # at most 6 s: keeps pure-python Goertzel fast
            row = [analyze.goertzel_db(seg, rate, f) for f in freqs]
        else:
            row = [-200.0] * len(freqs)
        matrix.append(row)
        o = row[i]
        m = o - max([v for j, v in enumerate(row) if j != i] or [-200])
        own.append(o)
        margin.append(m)
        passed &= (o > -40 and m >= 30)
    lines.append(f"S2 mode={a.mode} mute={a.mute} n={a.n} seconds={a.seconds}  (rows = tap i, cols = tone Hz, dBFS)")
    lines.append("tap " + " ".join(f"{f:>6d}" for f in freqs) + "  own   margin")
    for i, row in enumerate(matrix):
        lines.append(f"{i:>3d} " + " ".join(f"{v:6.1f}" for v in row) +
                     f"  {own[i]:5.1f} {margin[i]:6.1f}{'' if own[i] > -40 and margin[i] >= 30 else '  <-- FAIL'}")
    started = [h.find("started") for h in helpers]
    if started and started[0]:
        s = started[0][0]
        lines.append(f"tap0 started: rate={s.get('sample_rate')} ch={s.get('channels')} dev={s.get('device')}")
    lines.append(f"helper errors: {sum(len(h.find('error')) for h in helpers)}  silent events: "
                 f"{sum(len(h.find('audio_silent')) for h in helpers)}")
    if err:
        lines.append("run ended early: " + err)
    lines.append("criteria: every tap own > -40 dBFS and own - max(other) >= 30 dB")
    run.summary.update({"freqs": freqs, "matrix_db": matrix, "own_db": own, "margin_db": margin})
    run.finish(lines, bool(passed))


if __name__ == "__main__":
    main()
