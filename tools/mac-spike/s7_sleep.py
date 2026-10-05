#!/usr/bin/env python3
"""S7: display sleep and screen lock.  One Chrome window plays test_5min.mp4 while a helper captures
video (1920x1080, 30 fps) + audio (tap muted).  `caffeinate -d -i -w <our pid>` keeps the display awake.
Every second: helper complete fps / audio callbacks, display asleep (CoreGraphics), screen locked (ioreg).
Every 5 s: page rAF/rVFC counters.  Phases (time based, announced by notifications):
A baseline; B `pmset displaysleepnow`, sleep, wake, (unlock), after; C operator locks the screen manually.

PASS (required part) iff phase A has >= 29 complete fps in every second (first 5 s excluded) and the
caffeinate PreventUserIdleDisplaySleep assertion is present.  B/C are record-only."""
import argparse
import ctypes
import json
import os
import plistlib
import statistics
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from spikelib import runner, chrome as chromelib  # noqa: E402
from spikelib.runner import fmt  # noqa: E402

TITLE = "MC Spike S7"


# ---------------------------------------------------------------- pure parsers
def parse_assertions(text, owner="caffeinate", kind="PreventUserIdleDisplaySleep"):
    """Lines of `pmset -g assertions` that mention both `owner` and `kind`."""
    return [l.strip() for l in text.splitlines() if owner in l and kind in l]


def parse_locked(plist_bytes):
    """ioreg -n Root -d1 -a output -> True/False (CGSSessionScreenIsLocked of any console user) or None."""
    try:
        obj = plistlib.loads(plist_bytes)
    except Exception:
        return None
    found = []

    def walk(o):
        if isinstance(o, dict):
            users = o.get("IOConsoleUsers")
            if isinstance(users, list):
                for u in users:
                    if isinstance(u, dict):
                        found.append(bool(u.get("CGSSessionScreenIsLocked", False)))
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(obj)
    return any(found) if found else False


# ---------------------------------------------------------------- system probes
_cg = None


def display_asleep():
    global _cg
    try:
        if _cg is None:
            _cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
            _cg.CGMainDisplayID.restype = ctypes.c_uint32
            _cg.CGDisplayIsAsleep.argtypes = [ctypes.c_uint32]
            _cg.CGDisplayIsAsleep.restype = ctypes.c_int
        return bool(_cg.CGDisplayIsAsleep(_cg.CGMainDisplayID()))
    except Exception:
        return None


def screen_locked():
    try:
        out = subprocess.run(["ioreg", "-n", "Root", "-d1", "-a"], capture_output=True, timeout=5).stdout
        return parse_locked(out)
    except Exception:
        return None


def phase_stats(samples, stats_ev, polls, skip_first=0.0):
    """samples: [(t, phase, asleep, locked)]; stats_ev: [(t, complete, audio_cb)]; polls: [(t, counters)].
    Phases are looked up by time via the sample list."""
    ts = [s[0] for s in samples]

    def phase_at(t):
        import bisect
        i = bisect.bisect_right(ts, t) - 1
        return samples[i][1] if i >= 0 else None
    names = list(dict.fromkeys(s[1] for s in samples))
    out = {}
    for n in names:
        sel = [s for s in samples if s[1] == n]
        t_first = sel[0][0]
        ev = [e for e in stats_ev if phase_at(e[0]) == n and e[0] - t_first >= skip_first]
        comp = [e[1] for e in ev]
        cb = [e[2] for e in ev]
        d_v = d_r = d_t = 0.0
        for (ta, a), (tb, b) in zip(polls, polls[1:]):
            if phase_at(ta) == n and phase_at(tb) == n and tb > ta:
                d_v += b.get("vfc", 0) - a.get("vfc", 0)
                d_r += b.get("raf", 0) - a.get("raf", 0)
                d_t += tb - ta
        out[n] = {"seconds": len(sel), "asleep_s": sum(1 for s in sel if s[2]), "locked_s": sum(1 for s in sel if s[3]),
                  "stats_n": len(comp), "fps_mean": statistics.fmean(comp) if comp else None,
                  "fps_min": min(comp) if comp else None,
                  "cb_per_s": statistics.fmean(cb) if cb else None,
                  "vfc_per_s": d_v / d_t if d_t else None, "raf_per_s": d_r / d_t if d_t else None}
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--baseline", type=float, default=60)
    ap.add_argument("--sleep-seconds", type=float, default=60)
    ap.add_argument("--no-displaysleep", action="store_true", help="skip phase B")
    ap.add_argument("--no-lock", action="store_true", help="skip phase C")
    ap.add_argument("--pos", default="100,100", help="window position x,y")
    ap.add_argument("--size", default="1920x1080")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--video", default="test_5min.mp4")
    ap.add_argument("--skip-perm-check", action="store_true")
    a = ap.parse_args()
    cw, ch = runner.parse_size(a.size)
    px, py = map(int, a.pos.split(","))
    run = runner.Run("S7", None, a)
    run.preflight(need_screen=True, need_audio=True, skip=a.skip_perm_check)
    note = lambda t: runner.notify(t, TITLE, run.log)
    caff = None
    state = {"phase": "setup", "stop": False}
    samples, stats_ev, polls = [], [], []
    seen_stats = [0]
    err = None
    assertion_lines = []
    inst = h = None

    def sampler():
        while not state["stop"]:
            t = time.monotonic()
            samples.append((t, state["phase"], display_asleep(), screen_locked()))
            time.sleep(max(0.0, 1.0 - (time.monotonic() - t)))

    def poller():
        while not state["stop"]:
            t = time.monotonic()
            try:
                c = json.loads(inst.evaluate("JSON.stringify(window.__mc || null)", timeout=3) or "null")
                if c:
                    polls.append((t, c))
            except Exception:
                pass
            time.sleep(max(0.0, 5.0 - (time.monotonic() - t)))

    def collect_stats():
        evs = h.find("stats") if h else []
        for e in evs[seen_stats[0]:]:
            stats_ev.append((e["_recv"], e.get("complete", 0), e.get("audio_cb", 0)))
        seen_stats[0] = len(evs)

    def wait_until(cond, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            collect_stats()
            if cond():
                return True
            time.sleep(0.5)
        return False

    def record(secs):
        end = time.monotonic() + secs
        while time.monotonic() < end:
            collect_stats()
            time.sleep(0.5)

    def cur(i):
        return samples[-1][i] if samples else None

    try:
        url = run.player_url(src="/" + a.video, muted=0, loop=1, label="mc-s7")
        inst = run.launch_chrome("w0", url, px, py, cw, ch)
        run.log(f"playing={inst.wait_playing(30)} inner/outer={inst._sizes()}")
        wid = chromelib.find_window_id(inst.pid, "mc-s7")
        run.log(f"window_id={wid}")
        if wid is None:
            raise RuntimeError("window id not found")
        top = inst.content_inset()[1]
        caff = subprocess.Popen(["caffeinate", "-d", "-i", "-w", str(os.getpid())])
        time.sleep(1.5)
        asr = subprocess.run(["pmset", "-g", "assertions"], capture_output=True, text=True).stdout
        assertion_lines = parse_assertions(asr)
        run.log("caffeinate assertions: " + (" | ".join(assertion_lines) or "NONE"))
        open(os.path.join(run.dir, "pmset-assertions.txt"), "w").write(asr)
        hargs = runner.video_args(wid, (cw, ch), (0, top, cw, ch), a.fps, os.path.join(run.dir, "frames.csv"))
        hargs += ["--audio-tree", str(inst.pid), "--mute", "muted",
                  "--audio-out", os.path.join(run.dir, "tap.wav"), "--audio-log", os.path.join(run.dir, "tap.csv")]
        h = run.start_helper(hargs, "s7")
        run.log(f"first_frame={bool(h.wait_for('first_frame', 15))}")
        threading.Thread(target=sampler, daemon=True).start()
        threading.Thread(target=poller, daemon=True).start()

        state["phase"] = "A"
        note(f"フェーズA: 通常状態で{a.baseline:.0f}秒記録します。操作しないでください")
        record(a.baseline)

        if not a.no_displaysleep:
            state["phase"] = "B-asleep"
            note("フェーズB: これから画面をスリープさせます（pmset displaysleepnow）")
            subprocess.run(["pmset", "displaysleepnow"], capture_output=True)
            ok = wait_until(lambda: cur(2) is True, 15)
            run.log(f"display asleep detected: {ok}")
            record(a.sleep_seconds)
            state["phase"] = "B-after"
            subprocess.run(["caffeinate", "-u", "-t", "2"], capture_output=True)
            time.sleep(4)
            if cur(3):
                note("画面がロックされています。ロックを解除してください（最大180秒）")
            wait_until(lambda: cur(3) is False and cur(2) is False, 180)
            record(20)

        if not a.no_lock:
            state["phase"] = "C-wait"
            note("今から画面をロックしてください（Ctrl+Cmd+Q）。60秒後に解除してください")
            got = wait_until(lambda: cur(3) is True, 60)
            run.log(f"locked detected: {got}")
            if got:
                state["phase"] = "C-locked"
                wait_until(lambda: cur(3) is False, 180)
                state["phase"] = "C-after"
                note("ロックが解除されました。さらに20秒記録します")
                record(20)
            else:
                note("60秒以内にロックされませんでした。フェーズCを打ち切ります")
        state["phase"] = "end"
        record(1)
    except KeyboardInterrupt:
        err = "interrupted"
    except Exception as e:
        err = repr(e)
        run.log("ERROR " + err)
    finally:
        state["stop"] = True
        try:
            collect_stats()
        except Exception:
            pass
        run.stop_helpers()
        if caff:
            caff.terminate()
            try:
                caff.wait(3)
            except subprocess.TimeoutExpired:
                caff.kill()
        run.cleanup()

    # ---- analysis
    ps = phase_stats(samples, stats_ev, polls, 0.0)
    # phase A strict: skip the first 5 s
    a_t0 = next((s[0] for s in samples if s[1] == "A"), None)
    a_t1 = next((s[0] for s in samples if s[1] != "A" and a_t0 is not None and s[0] > a_t0), None)
    a_fps = [e[1] for e in stats_ev if a_t0 is not None and e[0] - a_t0 >= 5.0 and (a_t1 is None or e[0] < a_t1)]
    a_ok = bool(a_fps) and min(a_fps) >= 29
    caff_ok = bool(assertion_lines)
    passed = err is None and a_ok and caff_ok
    lines = [f"S7 baseline={a.baseline:.0f}s sleep={a.sleep_seconds:.0f}s  caffeinate assertion: "
             f"{'あり' if caff_ok else 'なし'}"]
    lines += ["  " + l[:110] for l in assertion_lines[:2]]
    lines.append("phase      secs asleep locked | fps mean/min | rVFC/s rAF/s | audio cb/s")
    for n in ("A", "B-asleep", "B-after", "C-locked", "C-after"):
        p = ps.get(n)
        if not p:
            lines.append(f"{n:<10} （未実施）")
            continue
        lines.append(f"{n:<10} {p['seconds']:4d} {p['asleep_s']:6d} {p['locked_s']:6d} | "
                     f"{fmt(p['fps_mean'])}/{fmt(p['fps_min'], 0)} | {fmt(p['vfc_per_s'])} {fmt(p['raf_per_s'])} | "
                     f"{fmt(p['cb_per_s'])}{'  (記録のみ)' if n != 'A' else ''}")
    if h:
        lines.append(f"stalls={len(h.find('stall'))} resumes={len(h.find('resume'))} errors={len(h.find('error'))} "
                     f"audio_silent={len(h.find('audio_silent'))} device_changed={len(h.find('audio_device_changed'))}")
    if a_fps:
        lines.append(f"phase A (skip 5 s): min fps {min(a_fps)} over {len(a_fps)} s -> {'OK' if a_ok else 'NG'}")
    if err:
        lines.append("run ended early: " + err)
    lines.append("criteria(required): phase A >= 29 fps every second AND caffeinate PreventUserIdleDisplaySleep present; B/C record only")
    run.summary.update({"phase_stats": ps, "assertion_lines": assertion_lines, "phase_A_fps": a_fps,
                        "samples": [(round(s[0] - run.t0, 1), s[1], s[2], s[3]) for s in samples]})
    run.finish(lines, bool(passed))


if __name__ == "__main__":
    main()
