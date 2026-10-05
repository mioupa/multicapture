"""Chrome launch / CDP / process discovery on macOS."""
import os
import re
import shlex
import signal
import subprocess
import time

from . import REPO_ROOT  # noqa: F401  (puts repo root on sys.path)
from multicapture import cdp

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
]

# Copied from multicapture/browser.py RENDER_FLAGS, minus the two flags merged below.
BASE_FLAGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--hide-crash-restore-bubble",
    "--disable-session-crashed-bubble",
    "--autoplay-policy=no-user-gesture-required",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-media-suspend",
    "--disable-sync",
]
BASE_DISABLE = ["CalculateNativeWinOcclusion", "IntensiveWakeUpThrottling", "msImplicitSignin"]
DSF1 = "--force-device-scale-factor=1"


def find_chrome():
    for c in CHROME_CANDIDATES:
        if os.path.isfile(c):
            return c
    raise RuntimeError("Google Chrome not found")


def _split_features(v):
    return [x for x in (v.split(",") if isinstance(v, str) else v) if x]


def build_args(profile_dir, url, x, y, w, h, app=True, extra_flags=(), disable_features=(),
               enable_features=(), dsf1=True):
    """Full argv (without the binary). All --disable-features / --enable-features are merged
    into ONE flag each (Chrome honors only the last one). `dsf1=False` drops
    --force-device-scale-factor=1."""
    disable = list(BASE_DISABLE)
    enable = []
    extras = []
    for f in list(extra_flags):
        if f.startswith("--disable-features="):
            disable += _split_features(f.split("=", 1)[1])
        elif f.startswith("--enable-features="):
            enable += _split_features(f.split("=", 1)[1])
        else:
            extras.append(f)
    disable += _split_features(disable_features)
    enable += _split_features(enable_features)
    dedup = lambda l: list(dict.fromkeys(l))
    args = list(BASE_FLAGS)
    args.append("--disable-features=" + ",".join(dedup(disable)))
    if enable:
        args.append("--enable-features=" + ",".join(dedup(enable)))
    if dsf1:
        args.append(DSF1)
    args += extras
    args += [
        f"--user-data-dir={profile_dir}",
        f"--window-position={x},{y}",
        f"--window-size={w},{h}",
        "--remote-debugging-port=0",
        f"--app={url}" if app else url,
    ]
    return args


def ps_table():
    out = subprocess.run(["ps", "-axo", "pid=,ppid=,command="], capture_output=True, text=True).stdout
    rows = []
    for line in out.splitlines():
        m = re.match(r"\s*(\d+)\s+(\d+)\s+(.*)$", line)
        if m:
            rows.append({"pid": int(m.group(1)), "ppid": int(m.group(2)), "cmd": m.group(3)})
    return rows


def _flag(cmd, name):
    m = re.search(r"--" + re.escape(name) + r"=(\S+)", cmd)
    return m.group(1) if m else None


def descendants_of(pid):
    rows = ps_table()
    kids = {}
    for r in rows:
        kids.setdefault(r["ppid"], []).append(r)
    out, stack = [], [pid]
    while stack:
        p = stack.pop()
        for r in kids.get(p, []):
            r = dict(r)
            r["type"] = _flag(r["cmd"], "type")
            r["utility_sub_type"] = _flag(r["cmd"], "utility-sub-type")
            out.append(r)
            stack.append(r["pid"])
    return out


def kill_profile_processes(root):
    """Terminate every process whose command line contains a --user-data-dir under `root`."""
    root = os.path.realpath(root)
    me = os.getpid()
    pids = []
    for r in ps_table():
        if r["pid"] == me:
            continue
        if ("--user-data-dir=" + root) in r["cmd"] or ("--user-data-dir=" + root.replace(os.sep + "private", "")) in r["cmd"]:
            pids.append(r["pid"])
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for p in pids:
            try:
                os.kill(p, sig)
            except OSError:
                pass
        time.sleep(1.0)
    return pids


def find_window_id(pid, title=None, helper_run=None):
    """Pick the on-screen, layer-0, largest window of `pid` using helper `windows --pid`."""
    if helper_run is None:
        from .helper import run_once
        helper_run = run_once
    best, best_key = None, None
    for e in helper_run(["windows", "--pid", pid], 20):
        if e.get("ev") != "window" or not e.get("on_screen") or e.get("layer", 0) != 0:
            continue
        fr = e.get("frame") or [0, 0, 0, 0]
        key = (1 if (title and title in (e.get("title") or "")) else 0, fr[2] * fr[3])
        if best_key is None or key > best_key:
            best, best_key = e, key
    return best["window_id"] if best else None


class ChromeInstance:
    def __init__(self, name, profile_dir, url, x=100, y=100, w=1920, h=1080, app=True,
                 extra_flags=(), disable_features=(), enable_features=(), dsf1=True, exe=None,
                 log_path=None):
        self.name, self.profile_dir, self.url = name, profile_dir, url
        self.x, self.y, self.w, self.h, self.app = x, y, w, h, app
        self.extra_flags, self.disable_features, self.enable_features = extra_flags, disable_features, enable_features
        self.dsf1 = dsf1
        self.exe = exe or find_chrome()
        self.log_path = log_path
        self.proc = None
        self.pid = None
        self.browser = None
        self.session = None
        self.target_id = None
        self.window_id_cdp = None
        self.args = None

    # --- lifecycle ---
    def launch(self, timeout=30.0):
        os.makedirs(self.profile_dir, exist_ok=True)
        try:
            os.remove(os.path.join(self.profile_dir, "DevToolsActivePort"))
        except OSError:
            pass
        self.args = build_args(self.profile_dir, self.url, self.x, self.y, self.w, self.h, self.app,
                               self.extra_flags, self.disable_features, self.enable_features, self.dsf1)
        log = open(self.log_path, "wb") if self.log_path else subprocess.DEVNULL
        self.proc = subprocess.Popen([self.exe, *self.args], stdout=log, stderr=log, stdin=subprocess.DEVNULL)
        self.pid = self.proc.pid  # on macOS the Popen pid is the browser main process
        ws = cdp.read_devtools_port(self.profile_dir, timeout)
        self.browser = cdp.Browser(ws)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pages = [t for t in self.browser.targets() if t.get("type") == "page"]
            if pages:
                self.target_id = pages[0]["targetId"]
                break
            time.sleep(0.2)
        else:
            raise RuntimeError(f"{self.name}: no page target")
        self.session = self.browser.attach(self.target_id)
        return self

    def evaluate(self, expr, timeout=15.0):
        return self.browser.evaluate(self.session, expr, timeout=timeout)

    def counters(self):
        import json
        return json.loads(self.evaluate("JSON.stringify(window.__mc || null)") or "null")

    def wait_playing(self, timeout=30.0):
        """Wait until the page shows advancing frames (vfc or raf increasing)."""
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                c = self.counters()
            except Exception:
                c = None
            if c:
                key = c.get("vfc", 0) if not c.get("anim") else c.get("raf", 0)
                if last is not None and key > last and key > 5:
                    return True
                last = key
            time.sleep(0.5)
        return False

    # --- window geometry ---
    def _window_id(self):
        r = self.browser.call("Browser.getWindowForTarget", {"targetId": self.target_id})
        self.window_id_cdp = r["windowId"]
        return r["windowId"], r.get("bounds", {})

    def sizes(self):
        return self.evaluate(
            "JSON.stringify([innerWidth,innerHeight,outerWidth,outerHeight,devicePixelRatio,screenX,screenY])")

    def _sizes(self):
        import json
        return json.loads(self.sizes())

    def set_content_size(self, w, h, tries=4):
        """Resize the window so innerWidth/innerHeight == (w,h). Returns (innerW, innerH)."""
        wid, bounds = self._window_id()
        iw, ih, ow, oh, _, _, _ = self._sizes()
        bw, bh = bounds.get("width", ow), bounds.get("height", oh)
        for _ in range(tries):
            iw, ih, ow, oh, _, _, _ = self._sizes()
            if (iw, ih) == (w, h):
                break
            # chrome width/height of the window beyond the content
            bw += w - iw
            bh += h - ih
            self.browser.call("Browser.setWindowBounds", {"windowId": wid, "bounds": {
                "windowState": "normal", "width": int(bw), "height": int(bh)}})
            time.sleep(0.4)
        iw, ih, *_ = self._sizes()
        return iw, ih

    def set_position(self, x, y):
        wid, _ = self._window_id()
        self.browser.call("Browser.setWindowBounds", {"windowId": wid, "bounds": {
            "windowState": "normal", "left": int(x), "top": int(y)}})

    def content_inset(self):
        """(left, top) of the content area inside the window. Assumes no side borders
        (macOS Chrome windows have none), so left = 0, top = outerHeight - innerHeight."""
        iw, ih, ow, oh, *_ = self._sizes()
        return (0, oh - ih)

    # --- processes ---
    def descendants(self):
        return descendants_of(self.pid)

    def audio_service_pid(self):
        for d in self.descendants():
            if "--utility-sub-type=audio.mojom.AudioService" in d["cmd"]:
                return d["pid"]
        return None

    def own_pids(self):
        return [self.pid] + [d["pid"] for d in self.descendants()] if self.pid else []

    def close(self):
        if self.proc is None:
            return
        pids = self.own_pids()
        try:
            if self.browser:
                try:
                    self.browser.call("Browser.close", timeout=3)
                except Exception:
                    pass
                self.browser.close()
        except Exception:
            pass
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                for p in pids:
                    try:
                        os.kill(p, sig)
                    except OSError:
                        pass
                try:
                    self.proc.wait(3)
                    break
                except subprocess.TimeoutExpired:
                    pass
        self.proc = None
