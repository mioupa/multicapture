#!/usr/bin/env python3
"""S9: Chrome profile under ~/Library/Application Support/MultiCapture-spike/<variant>/.
Steps: 1 login profile sets a persistent + a session cookie, graceful close; 2 relaunch -> persistent cookie
still there (disk persistence / keychain-backed encryption); 3 clone login -> seg1 (like browser.py
clone_profile), launch app-mode -> cookie carried over; 4 empty seg2 + CDP Storage.setCookies -> cookie present.
Throughout, a watcher looks (every 0.5 s) for keychain / authorization dialogs via helper `windows`, and
`security find-generic-password -s "Chrome Safe Storage"` (metadata only, no -w) is snapshotted before/after.

variant: default | mock (mock adds --use-mock-keychain --password-store=basic to ALL instances).
PASS iff all four steps pass and no keychain dialog was seen."""
import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from spikelib import runner, chrome as chromelib  # noqa: E402
from spikelib import helper as helperlib  # noqa: E402

# Copied from multicapture/browser.py (that module imports winreg, so it cannot be imported here).
PROFILE_SKIP = {
    "Cache", "Code Cache", "GPUCache", "DawnCache", "DawnGraphiteCache", "DawnWebGPUCache", "GrShaderCache",
    "GraphiteDawnCache", "ShaderCache", "component_crx_cache", "extensions_crx_cache", "Crashpad", "BrowserMetrics",
    "CacheStorage", "ScriptCache", "Safe Browsing", "optimization_guide_model_store", "OptimizationHints",
    "lockfile", "SingletonLock", "SingletonCookie", "SingletonSocket", "DevToolsActivePort", "BrowserMetrics-spare.pma",
}
ROOT_BASE = os.path.expanduser("~/Library/Application Support/MultiCapture-spike")
MOCK_FLAGS = ["--use-mock-keychain", "--password-store=basic"]
SUSPECT = ("securityagent", "coreautha", "keychain", "chrome safe storage", "authorization", "authenticat")


def clone_profile(src, dst):
    shutil.rmtree(dst, ignore_errors=True)
    if not os.path.isdir(src):
        os.makedirs(dst, exist_ok=True)
        return
    shutil.copytree(src, dst, ignore=lambda d, names: [n for n in names if n in PROFILE_SKIP], dirs_exist_ok=True,
                    ignore_dangling_symlinks=True)


def is_suspect_window(ev):
    """True for helper `window` events that look like keychain/authorization dialogs."""
    if ev.get("ev") != "window":
        return False
    s = " ".join(str(ev.get(k) or "") for k in ("bundle_id", "app", "title")).lower()
    return any(k in s for k in SUSPECT)


def keychain_snapshot():
    """Metadata of the 'Chrome Safe Storage' item (no -w, so no access prompt)."""
    try:
        p = subprocess.run(["security", "find-generic-password", "-s", "Chrome Safe Storage"],
                           capture_output=True, text=True, timeout=15)
    except Exception as e:
        return {"found": None, "error": repr(e)}
    keep = [l.strip() for l in p.stdout.splitlines() if any(k in l for k in ('"acct"', '"mdat"', '"cdat"', "keychain:"))]
    return {"found": p.returncode == 0, "rc": p.returncode, "attrs": keep, "stderr": p.stderr.strip()[:200]}


def cookie_header_has(text, name, value=None):
    try:
        cookie = json.loads(text).get("cookie", "")
    except Exception:
        return False
    for part in cookie.split(";"):
        k, _, v = part.strip().partition("=")
        if k == name and (value is None or v == value):
            return True
    return False


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--variant", choices=["default", "mock"], default="default")
    ap.add_argument("--clean", action="store_true", help="delete the variant's profile root first")
    ap.add_argument("--pos", default="200,200")
    ap.add_argument("--skip-perm-check", action="store_true")
    a = ap.parse_args()
    px, py = map(int, a.pos.split(","))
    root = os.path.join(ROOT_BASE, a.variant)
    flags = MOCK_FLAGS if a.variant == "mock" else []
    run = runner.Run("S9", a.variant, a)
    run.preflight(need_screen=True, skip=a.skip_perm_check)
    note = lambda t: runner.notify(t, "MC Spike S9", run.log)
    chromelib.kill_profile_processes(root)  # leftovers of ours only (matched by --user-data-dir path)
    if a.clean:
        shutil.rmtree(root, ignore_errors=True)
        run.log(f"cleaned {root}")
    os.makedirs(root, exist_ok=True)
    value = secrets.token_hex(8)
    steps, detections = {}, []
    state = {"step": "setup", "stop": False}
    insts = []
    err = None

    def watcher():
        seen = set()
        while not state["stop"]:
            t0 = time.monotonic()
            try:
                for e in helperlib.run_once(["windows"], 10):
                    if is_suspect_window(e):
                        key = (e.get("window_id"), e.get("bundle_id"), e.get("title"))
                        if key in seen:
                            continue
                        seen.add(key)
                        d = {"time": time.strftime("%H:%M:%S"), "step": state["step"], "window": e}
                        detections.append(d)
                        run.log(f"!! dialog-like window at step {d['step']}: {e.get('app')} {e.get('bundle_id')} {e.get('title')!r}")
            except Exception:
                pass
            time.sleep(max(0.0, 0.5 - (time.monotonic() - t0)))

    def launch(name, url, app):
        inst = chromelib.ChromeInstance(name, os.path.join(root, name), url, px, py, 900, 700, app, flags,
                                        log_path=os.path.join(run.dir, f"chrome-{name}.log"))
        insts.append(inst)
        inst.launch()
        return inst

    def close(inst):
        inst.close()
        time.sleep(1.0)  # let the profile settle on disk
        chromelib.kill_profile_processes(inst.profile_dir)

    def whoami(inst, navigate=False):
        if navigate:
            inst.browser.call("Page.navigate", {"url": run.base + "/whoami"}, session_id=inst.session)
        end = time.monotonic() + 10
        txt = ""
        while time.monotonic() < end:
            time.sleep(0.5)
            try:
                txt = inst.evaluate("document.body ? document.body.innerText : ''") or ""
            except Exception:
                continue
            if txt.strip().startswith("{") and "cookie" in txt:
                break
        return txt

    def cookie_names(cookies):
        return sorted(c["name"] for c in cookies)

    kc_before = keychain_snapshot()
    run.log(f"keychain before: {kc_before}")
    threading.Thread(target=watcher, daemon=True).start()
    cookies = []
    try:
        note(f"S9 開始（variant={a.variant}）。キーチェーンの許可ダイアログが出たら内容を控えてください")
        # step 1
        state["step"] = "1-set-cookie"
        q = urllib.parse.urlencode({"name": "mc_persist", "value": value, "max_age": 86400})
        login = launch("login", f"{run.base}/setcookie?{q}", app=False)
        time.sleep(1.5)
        login.browser.call("Page.navigate", {"url": f"{run.base}/setcookie?name=mc_session&value={value}"},
                           session_id=login.session)
        time.sleep(1.5)
        c1 = login.browser.get_cookies()
        ok1 = {"mc_persist", "mc_session"} <= set(cookie_names(c1))
        steps["1"] = {"desc": "login: cookies set + read via CDP, graceful close", "pass": ok1, "cookies": cookie_names(c1)}
        run.log(f"step1 cookies={cookie_names(c1)}")
        close(login)
        # step 2
        state["step"] = "2-relaunch"
        login = launch("login", run.base + "/whoami", app=False)
        txt = whoami(login)
        ok2 = cookie_header_has(txt, "mc_persist", value)
        cookies = login.browser.get_cookies()
        steps["2"] = {"desc": "relaunch login: persistent cookie survives", "pass": ok2,
                      "session_cookie_survived": cookie_header_has(txt, "mc_session"), "whoami": txt.strip()[:200]}
        run.log(f"step2 persist={ok2} session={steps['2']['session_cookie_survived']} {txt.strip()[:100]}")
        close(login)
        # step 3
        state["step"] = "3-clone"
        clone_profile(os.path.join(root, "login"), os.path.join(root, "seg1"))
        seg1 = launch("seg1", run.base + "/whoami", app=True)
        txt = whoami(seg1)
        ok3 = cookie_header_has(txt, "mc_persist", value)
        steps["3"] = {"desc": "clone login -> seg1 (app mode): cookie carried over", "pass": ok3, "whoami": txt.strip()[:200]}
        run.log(f"step3 persist={ok3}")
        close(seg1)
        # step 4
        state["step"] = "4-cdp-inject"
        shutil.rmtree(os.path.join(root, "seg2"), ignore_errors=True)
        seg2 = launch("seg2", "about:blank", app=True)
        seg2.browser.set_cookies(cookies)
        txt = whoami(seg2, navigate=True)
        ok4 = cookie_header_has(txt, "mc_persist", value)
        steps["4"] = {"desc": "empty seg2 + CDP setCookies: cookie present", "pass": ok4, "whoami": txt.strip()[:200]}
        run.log(f"step4 persist={ok4}")
        close(seg2)
        state["step"] = "done"
    except KeyboardInterrupt:
        err = "interrupted"
    except Exception as e:
        err = repr(e)
        run.log("ERROR " + err)
    finally:
        state["stop"] = True
        for inst in insts:
            try:
                inst.close()
            except Exception:
                pass
        try:
            chromelib.kill_profile_processes(root)
        except Exception:
            pass
        run.cleanup()
    kc_after = keychain_snapshot()

    lines = [f"S9 variant={a.variant} root={root}"]
    allok = err is None and all(steps.get(k, {}).get("pass") for k in "1234")
    for k in "1234":
        s = steps.get(k)
        lines.append(f"step {k}: {'PASS' if s and s['pass'] else ('FAIL' if s else '未実施')}  {s['desc'] if s else ''}")
    if "2" in steps:
        lines.append(f"  step2 のセッションCookie(mc_session)の再起動後の残存: {steps['2']['session_cookie_survived']}（参考）")
    lines.append(f"キーチェーン/認証ダイアログ検出: {'あり ' + str(len(detections)) + ' 件' if detections else 'なし'}")
    for d in detections[:5]:
        w = d["window"]
        lines.append(f"  {d['time']} step {d['step']}: {w.get('app')} / {w.get('bundle_id')} / {w.get('title')!r}")
    lines.append(f"Chrome Safe Storage 項目: 前={kc_before.get('found')} 後={kc_after.get('found')}"
                 + ("（Chrome が作成/参照した可能性）" if kc_before.get("found") != kc_after.get("found") else ""))
    for k, kc in (("前", kc_before), ("後", kc_after)):
        lines.append(f"  {k}: {' '.join(kc.get('attrs', []))[:150]}")
    if err:
        lines.append("run ended early: " + err)
    lines.append("criteria: steps 1-4 pass and no keychain dialog detected (OS 側で出たダイアログは目視でも確認)")
    run.summary.update({"steps": steps, "dialog_detections": detections, "keychain_before": kc_before,
                        "keychain_after": kc_after, "root": root})
    run.finish(lines, bool(allok and not detections))


if __name__ == "__main__":
    main()
