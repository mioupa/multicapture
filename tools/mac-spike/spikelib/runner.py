"""Shared plumbing for the sN_*.py runners: run dir, server, Chromes, helpers, polling, fps stats."""
import json
import os
import signal
import statistics
import subprocess
import sys
import threading
import time
import urllib.parse

from . import RESULTS_DIR, MEDIA_DIR
from . import chrome as chromelib
from . import helper as helperlib
from . import server as serverlib
from . import analyze


def parse_size(s):
    w, h = s.lower().split("x")
    return int(w), int(h)


def install_signal_handlers():
    def h(signum, frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, h)


class Run:
    def __init__(self, rid, variant=None, args=None, no_server=False):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        name = f"{rid}-{variant}-{stamp}" if variant else f"{rid}-{stamp}"
        self.id = rid
        self.dir = os.path.join(RESULTS_DIR, name)
        self.profiles = os.path.join(self.dir, "profiles")
        os.makedirs(self.profiles, exist_ok=True)
        self.chromes, self.helpers = [], []
        self.srv = self.base = None
        self._logf = open(os.path.join(self.dir, "run.log"), "a")
        self._evf = open(os.path.join(self.dir, "events.jsonl"), "a")
        self._evlock = threading.Lock()
        self.t0 = time.monotonic()
        self.summary = {"id": rid, "variant": variant, "args": vars(args) if args else {}, "dir": self.dir}
        install_signal_handlers()
        if not no_server:
            self.srv, self.base = serverlib.start()

    # -- logging
    def log(self, msg):
        line = f"[{time.monotonic() - self.t0:7.1f}s] {msg}"
        print(line, flush=True)
        self._logf.write(line + "\n")
        self._logf.flush()

    def _on_event(self, h, ev):
        with self._evlock:
            self._evf.write(json.dumps(ev) + "\n")
            self._evf.flush()

    # -- preflight
    def preflight(self, need_screen=True, need_audio=False, skip=False):
        if not os.path.exists(helperlib.HELPER):
            sys.exit(f"helper not built: {helperlib.HELPER} (run tools/mac-spike/build_helper.sh)")
        if skip:
            return
        ev = helperlib.run_once(["perm"], 15)
        perm = next((e for e in ev if e.get("ev") == "perm"), {})
        self.summary["perm"] = perm
        bad = (need_screen and not perm.get("screen")) or (need_audio and perm.get("audio") == "denied")
        if bad:
            sys.exit(f"TCC permission missing: {perm}. Run `{helperlib.HELPER} perm --request` first "
                     "(or pass --skip-perm-check).")

    # -- chrome
    def player_url(self, **params):
        return self.base + "/player.html?" + urllib.parse.urlencode(params)

    def launch_chrome(self, name, url, x, y, content_w, content_h, extra_flags=(), disable_features=(),
                      enable_features=(), dsf1=True, app=True, set_size=True):
        inst = chromelib.ChromeInstance(name, os.path.join(self.profiles, name), url, x, y, content_w,
                                        content_h + 100, app, extra_flags, disable_features, enable_features,
                                        dsf1, log_path=os.path.join(self.dir, f"chrome-{name}.log"))
        self.chromes.append(inst)  # register first so cleanup always sees it
        inst.launch()
        if set_size:
            got = inst.set_content_size(content_w, content_h)
            if got != (content_w, content_h):
                self.log(f"{name}: content size wanted {content_w}x{content_h}, got {got[0]}x{got[1]}")
        return inst

    # -- helper
    def start_helper(self, args, label):
        h = helperlib.Helper(args, label, os.path.join(self.dir, f"helper-{label}.stderr"), self._on_event)
        self.helpers.append(h)
        return h

    def stop_helpers(self, hs=None):
        hs = self.helpers if hs is None else hs
        threads = [threading.Thread(target=h.stop) for h in hs]
        for t in threads:
            t.start()
        for t in threads:
            t.join(15)

    # -- cleanup / summary
    def cleanup(self):
        try:
            self.stop_helpers()
        except Exception:
            pass
        for c in self.chromes:
            try:
                c.close()
            except Exception:
                pass
        try:
            chromelib.kill_profile_processes(self.profiles)
        except Exception:
            pass
        if self.srv:
            try:
                serverlib.stop(self.srv)
            except Exception:
                pass

    def finish(self, lines, passed=None):
        self.summary["pass"] = passed
        with open(os.path.join(self.dir, "summary.json"), "w") as f:
            json.dump(self.summary, f, indent=1, default=str)
        print("\n=== " + self.id + " summary (" + self.dir + ") ===")
        for l in lines[:30]:
            print(l)
        if passed is not None:
            print("RESULT: " + ("PASS" if passed else "FAIL"))
        with open(os.path.join(self.dir, "summary.txt"), "w") as f:
            f.write("\n".join(lines) + "\n")


def video_args(window_id, size, crop, fps, frame_log, extra=()):
    a = ["capture", "--window-id", window_id, "--size", f"{size[0]}x{size[1]}", "--fps", fps,
         "--crop", ",".join(map(str, crop)), "--frame-log", frame_log]
    return a + list(extra)


def sleep_interruptible(seconds, tick=0.2):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        time.sleep(min(tick, max(0, end - time.monotonic())))


# ---------------------------------------------------------------- stats
def fps_stats(rows, skip=5.0, key="pts", thresh=29.0, window=10):
    """From frame-log rows -> dict(mean, min_win, pct_bins_low, bins, frames). Time axis starts
    at the first row; the first `skip` seconds and a trailing partial second are excluded."""
    ts = [r[key] for r in rows]
    if len(ts) < 2:
        return {"frames": len(ts), "mean": 0.0, "min_win": 0.0, "pct_bins_low": 100.0, "bins": 0}
    t0, tend = ts[0], ts[-1]
    nb = int(tend - t0)
    counts = [0] * max(nb, 0)
    for t in ts:
        b = int(t - t0)
        if 0 <= b < nb:
            counts[b] += 1
    use = counts[int(skip):]
    if not use:
        return {"frames": len(ts), "mean": 0.0, "min_win": 0.0, "pct_bins_low": 100.0, "bins": 0}
    mean = sum(use) / len(use)
    w = min(window, len(use))
    min_win = min(sum(use[i:i + w]) / w for i in range(len(use) - w + 1))
    low = sum(1 for c in use if c < thresh)
    return {"frames": len(ts), "mean": mean, "min_win": min_win, "pct_bins_low": 100.0 * low / len(use),
            "bins": len(use), "min_bin": min(use), "low_bins": low}


def rate_from_polls(polls, key, skip=5.0):
    """polls: list of (t_monotonic, counters dict). -> (mean rate/s, min rate/s) over successive polls."""
    if len(polls) < 2:
        return None, None
    t0 = polls[0][0]
    rates = []
    for (ta, a), (tb, b) in zip(polls, polls[1:]):
        if ta - t0 < skip or tb <= ta:
            continue
        rates.append((b[key] - a[key]) / (tb - ta))
    if not rates:
        return None, None
    return statistics.fmean(rates), min(rates)


def ps_sample(pids):
    """-> {pid: (cpu%, rss_kb)} via ps."""
    pids = [p for p in pids if p]
    if not pids:
        return {}
    out = subprocess.run(["ps", "-o", "pid=,%cpu=,rss=", "-p", ",".join(map(str, pids))],
                         capture_output=True, text=True).stdout
    res = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3:
            res[int(parts[0])] = (float(parts[1]), int(parts[2]))
    return res


def pids_by_name(name):
    out = subprocess.run(["pgrep", "-x", name], capture_output=True, text=True).stdout
    return [int(x) for x in out.split()]


def fmt(v, nd=1):
    return "n/a" if v is None else f"{v:.{nd}f}"


def notify(text, title="MC Spike", log=None):
    """Print `text` with a wall-clock stamp and show a macOS notification (osascript). Never raises."""
    line = f"[{time.strftime('%H:%M:%S')}] {title}: {text}"
    if log:
        log(line)
    else:
        print(line, flush=True)
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    try:
        subprocess.run(["osascript", "-e", f'display notification "{esc(text)}" with title "{esc(title)}"'],
                       capture_output=True, timeout=10)
    except Exception:
        pass
