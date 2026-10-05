#!/usr/bin/env python3
"""S3: muted tap behaviour.  One Chrome window plays a 440 Hz tone; phases of --phase-seconds each:
P0 no tap (audible) / P1 tap unmuted (audible) / P2 tap muted (silent) / P3 tap mutedWhenTapped (silent)
/ P4 helper stopped (audible).  With --system-volume-test also P5 (system muted + tap) and P6 (system
volume 0 + tap); the original volume settings are always restored.  Phase changes are announced by macOS
notifications; the operator reports what was audible.

PASS (recording part) iff every tapped phase has a 440 Hz level > -30 dBFS. The audible part is not measured."""
import argparse
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from spikelib import runner, analyze  # noqa: E402

TITLE = "MC Spike S3"
FREQ = 440
THRESH_DB = -30.0


def parse_volume_settings(text):
    """'output volume:50, input volume:missing value, alert volume:100, output muted:false' -> dict.
    Values: int, bool, or None for 'missing value'."""
    d = {}
    for part in text.strip().split(", "):
        k, _, v = part.partition(":")
        v = v.strip()
        if v == "missing value":
            val = None
        elif v in ("true", "false"):
            val = v == "true"
        else:
            try:
                val = int(v)
            except ValueError:
                val = v
        d[k.strip()] = val
    return d


def get_volume():
    out = subprocess.run(["osascript", "-e", "get volume settings"], capture_output=True, text=True, timeout=10)
    return parse_volume_settings(out.stdout)


def osa(script):
    return subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10)


def analyze_wav(path, skip=2.0, max_s=4.0):
    """-> dict(rate, db440, peak_db, seconds). Looks at the middle part, after `skip` seconds."""
    if not path or not os.path.exists(path):
        return {"rate": None, "db440": -200.0, "peak_db": -200.0, "seconds": 0.0}
    try:
        rate, ch, x = analyze.read_wav(path)
    except Exception as e:
        return {"rate": None, "db440": -200.0, "peak_db": -200.0, "seconds": 0.0, "error": repr(e)}
    n = len(x)
    a = min(int(skip * rate), n // 4)
    seg = x[a:a + int(max_s * rate)] if n - a > 0 else x
    seg = seg[:int(max_s * rate)]
    if len(x) == 0:
        return {"rate": rate, "db440": -200.0, "peak_db": -200.0, "seconds": 0.0}
    pk = max(max(seg), -min(seg)) if len(seg) else 0.0
    import math
    return {"rate": rate, "db440": analyze.goertzel_db(seg, rate, FREQ),
            "peak_db": 20 * math.log10(pk) if pk > 1e-10 else -200.0, "seconds": n / rate}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phase-seconds", type=float, default=8)
    ap.add_argument("--system-volume-test", action="store_true",
                    help="also test with system output muted / volume 0 (changes and restores the system volume)")
    ap.add_argument("--gain", type=float, default=0.3)
    ap.add_argument("--pos", default="200,200", help="window position x,y")
    ap.add_argument("--skip-perm-check", action="store_true")
    a = ap.parse_args()
    px, py = map(int, a.pos.split(","))
    run = runner.Run("S3", None, a)
    run.preflight(need_screen=False, need_audio=True, skip=a.skip_perm_check)
    note = lambda t: runner.notify(t, TITLE, run.log)
    phases = []  # dicts
    err = None
    vol_orig = None
    vol_note = None
    inst = None

    def tapped_phase(pid_, name, expect, mute, secs, label):
        wav = os.path.join(run.dir, f"{label}.wav")
        csv_ = os.path.join(run.dir, f"{label}.csv")
        t0 = time.strftime("%H:%M:%S")
        h = run.start_helper(["capture", "--audio-tree", inst.pid, "--audio-out", wav, "--audio-log", csv_,
                              "--mute", mute], label)
        st = h.wait_for("started", 15)
        run.log(f"{label}: started={bool(st)} audio={(st or {}).get('audio')}")
        runner.sleep_interruptible(secs)
        h.stop()
        phases.append({"phase": pid_, "name": name, "expect": expect, "mute": mute, "start": t0, "wav": wav,
                       "started": (st or {}).get("audio"), "errors": len(h.find("error")),
                       "silent_events": len(h.find("audio_silent"))})

    try:
        url = run.player_url(anim=1, tone=FREQ, gain=a.gain, label="mc-s3")
        inst = run.launch_chrome("w0", url, px, py, 640, 360)
        run.log(f"playing={inst.wait_playing(30)} audioState={(inst.counters() or {}).get('audioState')}")
        time.sleep(1)
        S = a.phase_seconds
        note(f"P0 ベースライン（タップなし）。音が聞こえるはず（{S:.0f}秒）")
        phases.append({"phase": "P0", "name": "baseline", "expect": "聞こえる", "mute": None,
                       "start": time.strftime("%H:%M:%S"), "wav": None})
        runner.sleep_interruptible(S)
        note("P1 タップ unmuted。音が聞こえるはず")
        tapped_phase("P1", "tap unmuted", "聞こえる", "unmuted", S, "p1")
        note("P2 タップ muted。音は聞こえないはず")
        tapped_phase("P2", "tap muted", "聞こえない", "muted", S, "p2")
        note("P3 タップ mutedWhenTapped。音は聞こえないはず")
        tapped_phase("P3", "tap mutedWhenTapped", "聞こえない", "mutedWhenTapped", S, "p3")
        note("P4 ヘルパー停止後。音が聞こえるはず")
        phases.append({"phase": "P4", "name": "helper stopped", "expect": "聞こえる", "mute": None,
                       "start": time.strftime("%H:%M:%S"), "wav": None})
        runner.sleep_interruptible(S)
        if a.system_volume_test:
            vol_orig = get_volume()
            run.summary["volume_original"] = vol_orig
            if vol_orig.get("output volume") is None:
                vol_note = "N/A（出力デバイスが音量調整に非対応）"
                run.log("system volume test: " + vol_note)
            else:
                note("P5 システムをミュート中にタップ(unmuted)。何も聞こえないはず")
                osa("set volume with output muted")
                tapped_phase("P5", "system muted + tap unmuted", "聞こえない", "unmuted", S, "p5")
                note("P6 システム音量0にしてタップ(unmuted)。何も聞こえないはず")
                osa("set volume without output muted")
                osa("set volume output volume 0")
                tapped_phase("P6", "system volume 0 + tap unmuted", "聞こえない", "unmuted", S, "p6")
    except KeyboardInterrupt:
        err = "interrupted"
    except Exception as e:
        err = repr(e)
        run.log("ERROR " + err)
    finally:
        run.stop_helpers()
        if vol_orig and vol_orig.get("output volume") is not None:
            try:
                osa(f"set volume output volume {int(vol_orig['output volume'])}")
                osa("set volume " + ("with" if vol_orig.get("output muted") else "without") + " output muted")
                run.log(f"system volume restored: {get_volume()}")
            except Exception as e:
                run.log(f"!! FAILED to restore volume {vol_orig}: {e!r}")
        run.cleanup()

    # ---- analysis
    passed = err is None
    lines = [f"S3 tone={FREQ}Hz phase={a.phase_seconds:.0f}s  (tapped phases are recorded; audibility is for the operator)"]
    lines.append("フェーズ | 期待 | タップの440Hzレベル(dBFS) / peak | 開始時刻")
    for p in phases:
        if p.get("wav"):
            r = analyze_wav(p["wav"])
            p.update(r)
            ok = r["db440"] > THRESH_DB
            p["rec_ok"] = ok
            passed &= ok
            lv = f"{r['db440']:6.1f} / {r['peak_db']:6.1f}  rate={r['rate']}{'' if ok else '  <-- 録音NG'}"
        else:
            lv = "（タップなし）"
        lines.append(f"{p['phase']} {p['name']} | {p['expect']} | {lv} | {p['start']}")
    expected = ["P0", "P1", "P2", "P3", "P4"]
    if [p["phase"] for p in phases][:5] != expected:
        passed = False
    if a.system_volume_test:
        lines.append("システム音量テスト: " + (vol_note or f"実施（元の設定 {vol_orig} に復元）"))
    if err:
        lines.append("run ended early: " + err)
    lines.append("聞こえた/聞こえなかったフェーズを報告してください（期待と食い違いがあれば教えてください）。")
    lines.append(f"criteria(recording): every tapped phase 440 Hz > {THRESH_DB:.0f} dBFS")
    run.summary.update({"phases": phases, "system_volume": vol_note})
    run.finish(lines, bool(passed))


if __name__ == "__main__":
    main()
