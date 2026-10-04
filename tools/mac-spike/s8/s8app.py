#!/usr/bin/env python3
"""S8 test app: permission / unsigned-distribution probe (stdlib only)."""
import argparse, json, os, plistlib, platform, struct, subprocess, sys, tempfile, time, wave, math

try:
    import _s8_version
    EMBED_VERSION = _s8_version.VERSION
except Exception:
    EMBED_VERSION = "dev"

# S8 tests whether the helper inherits this app's permission, so it must not disclaim responsibility.
os.environ["MCSPIKE_NO_DISCLAIM"] = "1"

STAMP = time.strftime("%Y%m%d-%H%M%S")
LOGDIR = os.path.expanduser("~/Library/Logs/MultiCaptureSpike")
os.makedirs(LOGDIR, exist_ok=True)
LOGPATH = os.path.join(LOGDIR, f"s8-{STAMP}.log")
RESPATH = os.path.join(LOGDIR, f"s8-{STAMP}.json")
_logf = open(LOGPATH, "a", encoding="utf-8")
result = {"stamp": STAMP, "embedded_version": EMBED_VERSION, "steps": {}}


def log(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    _logf.write(line + "\n"); _logf.flush()
    print(line, file=sys.stderr, flush=True)


def run(cmd, timeout=120, **kw):
    log("$ " + " ".join(map(str, cmd)))
    # A Finder-launched app has stdin at /dev/null; the helper stops on stdin EOF,
    # so hand it a pipe whose write end stays open until the command returns.
    r, w = os.pipe()
    try:
        p = subprocess.run(cmd, stdin=r, capture_output=True, text=True, timeout=timeout, **kw)
        log(f"  rc={p.returncode}")
        if p.stdout.strip(): log("  stdout: " + p.stdout.strip()[:4000])
        if p.stderr.strip(): log("  stderr: " + p.stderr.strip()[:4000])
        return p
    except Exception as e:
        log(f"  EXC {e!r}")
        return None
    finally:
        os.close(r)
        os.close(w)


def jlines(text):
    out = []
    for l in (text or "").splitlines():
        try: out.append(json.loads(l))
        except Exception: pass
    return out


def bundle_path():
    exe = os.path.realpath(sys.executable)
    marker = ".app/Contents/"
    i = exe.find(marker)
    return exe[:i + 4] if i >= 0 else None


def bundle_info(bp):
    info = {"bundle_path": bp}
    if not bp:
        info["sign_mode"] = "n/a (not a bundle)"
        return info
    try:
        with open(os.path.join(bp, "Contents/Info.plist"), "rb") as f:
            pl = plistlib.load(f)
        info["version"] = pl.get("CFBundleShortVersionString")
        info["build"] = pl.get("CFBundleVersion")
        info["bundle_id"] = pl.get("CFBundleIdentifier")
    except Exception as e:
        info["plist_error"] = repr(e)
    p = run(["codesign", "-dvvv", bp])
    txt = p.stderr if p else ""
    info["codesign_raw"] = txt
    info["identifier"] = next((l.split("=", 1)[1] for l in txt.splitlines() if l.startswith("Identifier=")), None)
    info["cdhash"] = next((l.split("=", 1)[1] for l in txt.splitlines() if l.startswith("CDHash=")), None)
    auth = [l.split("=", 1)[1] for l in txt.splitlines() if l.startswith("Authority=")]
    info["authority"] = auth
    adhoc = "Signature=adhoc" in txt
    info["sign_mode"] = "adhoc" if adhoc else ("self/cert: " + (auth[0] if auth else "unknown") if auth else "unsigned/unknown")
    q = run(["xattr", "-p", "com.apple.quarantine", bp])
    info["quarantine"] = q.stdout.strip() if q and q.returncode == 0 else None
    return info


def find_helper(explicit):
    if explicit:
        return explicit
    cands = []
    mp = getattr(sys, "_MEIPASS", None)
    if mp: cands.append(os.path.join(mp, "mc-capture"))
    exedir = os.path.dirname(os.path.realpath(sys.executable))
    cands += [os.path.join(exedir, "mc-capture"),
              os.path.join(exedir, "..", "Frameworks", "mc-capture"),
              os.path.join(exedir, "..", "Resources", "mc-capture"),
              os.path.join(exedir, "_internal", "mc-capture")]
    for c in cands:
        if os.path.isfile(c):
            return os.path.realpath(c)
    log("helper not found; tried: " + ", ".join(cands))
    return None


def helper(h, args, timeout=120):
    p = run([h] + args, timeout=timeout)
    return (jlines(p.stdout) if p else []), (p.returncode if p else None)


def perm(h, request=False):
    evs, rc = helper(h, ["perm"] + (["--request"] if request else []), timeout=120 if request else 30)
    last = next((e for e in reversed(evs) if e.get("ev") == "perm"), None)
    return {"rc": rc, "perm": last, "events": evs}


def granted(p):
    pm = (p or {}).get("perm") or {}
    return bool(pm.get("screen")), pm.get("audio") == "granted"


def pick_window(h, own_pid):
    evs, _ = helper(h, ["windows"], timeout=60)
    wins = [e for e in evs if e.get("ev") == "window" and e.get("on_screen") and e.get("layer") == 0
            and e.get("pid") != own_pid and (e.get("frame") or [0, 0, 0, 0])[2] >= 100 and e["frame"][3] >= 100]
    wins.sort(key=lambda e: (e.get("bundle_id") != "com.apple.finder", -e["frame"][2] * e["frame"][3]))
    return wins[0] if wins else None


def do_video(h):
    r = {}
    w = pick_window(h, os.getpid())
    if not w:
        r["error"] = "no suitable window"; return r
    fw, fh = int(w["frame"][2]), int(w["frame"][3])
    fw -= fw % 2; fh -= fh % 2
    r["window"] = {k: w.get(k) for k in ("window_id", "pid", "bundle_id", "app", "title", "frame")}
    evs, rc = helper(h, ["capture", "--window-id", str(w["window_id"]), "--size", f"{fw}x{fh}", "--duration", "2"], timeout=60)
    r["rc"] = rc
    r["complete_frames"] = sum(e.get("complete", 0) for e in evs if e.get("ev") == "stats")
    r["errors"] = [e for e in evs if e.get("ev") == "error"]
    return r


def make_tone(path, secs=6, hz=440, rate=44100):
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(12000 * math.sin(2 * math.pi * hz * i / rate))) for i in range(rate * secs)))


def wav_peak(path):
    with wave.open(path, "rb") as w:
        n = w.getnframes(); data = w.readframes(n)
        ch, sw = w.getnchannels(), w.getsampwidth()
    if sw != 2 or not data: return 0.0
    vals = struct.unpack("<%dh" % (len(data) // 2), data)
    return max(abs(v) for v in vals) / 32768.0


def do_audio(h, tmp):
    r = {}
    tone = os.path.join(tmp, "tone.wav"); out = os.path.join(tmp, "cap.wav")
    make_tone(tone)
    ap = subprocess.Popen(["afplay", tone])
    try:
        time.sleep(0.5)
        evs, rc = helper(h, ["capture", "--audio-pid", str(ap.pid), "--duration", "3", "--audio-out", out, "--mute", "unmuted"], timeout=60)
        r["rc"] = rc
        r["errors"] = [e for e in evs if e.get("ev") == "error"]
        r["stats_audio_peak"] = max([e.get("audio_peak", 0) for e in evs if e.get("ev") == "stats"] or [0])
        try: r["wav_peak"] = wav_peak(out)
        except Exception as e: r["wav_peak"] = None; r["wav_error"] = repr(e)
    finally:
        ap.terminate()
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--helper")
    a = ap.parse_args()
    log(f"S8 start pid={os.getpid()} argv={sys.argv}")
    result["macos"] = platform.mac_ver()[0]
    bp = bundle_path()
    info = bundle_info(bp)
    result["bundle"] = info
    h = find_helper(a.helper)
    result["helper"] = h
    summary = {"frames": "-", "peak": "-", "screen": "?", "audio": "?"}
    if not h:
        result["error"] = "helper not found"
    else:
        p1 = perm(h); result["steps"]["perm1"] = p1
        sg, ag = granted(p1)
        if not (sg and ag) and not a.dry_run:
            result["steps"]["perm_request"] = perm(h, True)
            p2 = perm(h); result["steps"]["perm2"] = p2
            sg, ag = granted(p2)
        final = (result["steps"].get("perm2") or p1).get("perm") or {}
        summary["screen"] = "許可済み" if sg else "未許可"
        summary["audio"] = {"granted": "許可済み", "denied": "拒否"}.get(final.get("audio"), "不明")
        if not a.dry_run:
            if sg:
                result["steps"]["video"] = v = do_video(h)
                summary["frames"] = str(v.get("complete_frames", "error"))
            if ag:
                with tempfile.TemporaryDirectory() as tmp:
                    result["steps"]["audio"] = au = do_audio(h, tmp)
                pk = au.get("wav_peak")
                summary["peak"] = f"{pk:.3f}" if pk is not None else "error"
    result["summary"] = summary
    with open(RESPATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    log(f"result: {RESPATH}")
    ver = info.get("version") or EMBED_VERSION
    msg = (f"バージョン: {ver}\\n署名: {info.get('sign_mode')}\\n"
           f"画面収録: {summary['screen']}\\nシステム音声: {summary['audio']}\\n"
           f"取得フレーム数: {summary['frames']}\\n音声ピーク: {summary['peak']}\\n\\nログ: {LOGPATH}")
    msg = msg.replace('"', "'")
    if not a.dry_run:
        run(["osascript", "-e", f'display dialog "{msg}" with title "MultiCapture Spike" buttons {{"OK"}} default button 1'], timeout=600)
    else:
        log("dry-run summary: " + msg.replace("\\n", " | "))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        log("FATAL " + traceback.format_exc())
        try:
            run(["osascript", "-e", f'display dialog "エラー: {str(e)[:200].replace(chr(34), chr(39))}" with title "MultiCapture Spike" buttons {{"OK"}} default button 1'])
        except Exception: pass
        sys.exit(1)
