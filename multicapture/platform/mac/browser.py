"""BrowserWindow for macOS: Chrome/Edge launched directly, sized and inspected over CDP."""
import json
import os
import subprocess
import time

from ...browser_common import RENDER_FLAGS
from ...cdp import Browser, CDPError, read_devtools_port
from . import helper

CACHE_SECONDS = 1.0


class BrowserWindow:
    def __init__(self, exe_path, profile_dir, url, x, y, width, height, app=True, debug=False):
        self.exe_path = exe_path
        self.profile_dir = profile_dir
        self.url = url
        self.x = x
        self.y = y
        self.width = width
        self.height = height
        self.app = app
        self.debug = debug   # macOS always opens the debugging port (needed for sizing)
        self.process = None
        self.pid = None
        self.cdp = None
        self.target_id = None
        self.session = None
        self.window_id = None          # CGWindowID for ScreenCaptureKit
        self.helper_session = None     # shared by the window and audio capture adapters
        self._offset = None
        self._offset_at = 0.0
        self._alive_at = 0.0
        self._alive = True
        self._no_page = 0

    # ------------------------------------------------------------------ launch
    def devtools_url(self, timeout=20.0):
        return read_devtools_port(self.profile_dir, timeout)

    def launch(self, timeout=30.0):
        os.makedirs(self.profile_dir, exist_ok=True)
        try:
            os.remove(os.path.join(self.profile_dir, "DevToolsActivePort"))
        except OSError:
            pass
        args = [
            self.exe_path,
            f"--user-data-dir={self.profile_dir}",
            *RENDER_FLAGS,
            f"--window-position={self.x},{self.y}",
            f"--window-size={self.width},{self.height}",
            "--remote-debugging-port=0",
            f"--app={self.url}" if self.app else self.url,
        ]
        self.process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
        self.pid = self.process.pid
        deadline = time.monotonic() + timeout
        ws = None
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(
                    "ブラウザがすぐに終了しました。同じプロフィールのブラウザがすでに起動している可能性があります。"
                    "開いているブラウザを閉じてから、もう一度お試しください。")
            try:
                ws = read_devtools_port(self.profile_dir, 0.3)
                break
            except CDPError:
                continue
        if ws is None:
            raise RuntimeError("ブラウザの制御ポートが見つかりませんでした")
        self.cdp = Browser(ws)
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("ブラウザが起動中に終了しました")
            pages = [t for t in self.cdp.targets() if t.get("type") == "page"]
            if pages:
                self.target_id = pages[0]["targetId"]
                break
            time.sleep(0.2)
        else:
            raise RuntimeError("ブラウザのウィンドウが見つかりませんでした")
        self.session = self.cdp.attach(self.target_id)

    def find_window_id(self, timeout=15.0):
        """CGWindowID of the browser window (on-screen, layer 0, largest), via the control helper."""
        if self.window_id:
            return self.window_id
        deadline = time.monotonic() + timeout
        while True:
            wid = helper.pick_window(helper.list_windows(self.pid))
            if wid:
                self.window_id = wid
                return wid
            if time.monotonic() > deadline:
                raise RuntimeError("ブラウザのウィンドウが見つかりませんでした（画面収録の許可を確認してください）")
            time.sleep(0.3)

    # ------------------------------------------------------------------ CDP helpers
    def _call(self, method, params=None, timeout=5.0):
        return self.cdp.call(method, params, timeout=timeout)

    def _page_call(self, fn):
        """Run fn(session); on failure re-attach to the page target once."""
        try:
            return fn(self.session)
        except CDPError:
            pages = [t for t in self.cdp.targets() if t.get("type") == "page"]
            if not pages:
                raise
            self.target_id = pages[0]["targetId"]
            self.session = self.cdp.attach(self.target_id)
            return fn(self.session)

    def _sizes(self):
        raw = self._page_call(lambda s: self.cdp.evaluate(
            s, "JSON.stringify([innerWidth,innerHeight,outerWidth,outerHeight])", timeout=5))
        return json.loads(raw)

    def _cdp_window_id(self):
        return self._page_call(lambda s: self._call("Browser.getWindowForTarget", {"targetId": self.target_id}))["windowId"]

    # ------------------------------------------------------------------ BrowserWindow API
    def alive(self):
        if self.process is None or self.process.poll() is not None:
            return False
        now = time.monotonic()
        if self.cdp is None or now - self._alive_at < CACHE_SECONDS:
            return self._alive
        self._alive_at = now
        try:
            pages = [t for t in self.cdp.targets() if t.get("type") == "page"]
        except CDPError as exc:
            # a connection that is gone means the browser is; a timeout may just be a busy browser
            if "closed" in str(exc):
                self._alive = False
            return self._alive
        except OSError:
            self._alive = False
            return False
        self._no_page = 0 if pages else self._no_page + 1
        self._alive = self._no_page < 2
        return self._alive

    def fit_content(self, width, height, attempts=3):
        wid = self._cdp_window_id()
        for _ in range(attempts):
            iw, ih, _, _ = self._sizes()
            if (iw, ih) == (width, height):
                return iw, ih
            bounds = self._call("Browser.getWindowBounds", {"windowId": wid}).get("bounds", {})
            self._call("Browser.setWindowBounds", {"windowId": wid, "bounds": {
                "windowState": "normal",
                "width": int(bounds.get("width", self.width) + width - iw),
                "height": int(bounds.get("height", self.height) + height - ih)}})
            time.sleep(0.4)
        self._offset = None
        iw, ih, _, _ = self._sizes()
        return iw, ih

    def capture_offset(self):
        now = time.monotonic()
        if self._offset is not None and now - self._offset_at < CACHE_SECONDS:
            return self._offset
        try:
            _, ih, _, oh = self._sizes()
        except (CDPError, OSError, ValueError):
            return None
        self._offset = (0, max(0, int(oh - ih)))
        self._offset_at = now
        return self._offset

    def restore_if_minimized(self):
        try:
            wid = self._cdp_window_id()
            state = self._call("Browser.getWindowBounds", {"windowId": wid}).get("bounds", {}).get("windowState")
            if state == "minimized":
                self._call("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "normal"}})
        except (CDPError, OSError):
            pass

    def close(self, timeout=8.0):
        proc = self.process
        if self.cdp is not None:
            try:
                self.cdp.call("Browser.close", timeout=3)
            except (CDPError, OSError):
                pass
            try:
                self.cdp.close()
            except OSError:
                pass
            self.cdp = None
        if proc is not None:
            try:
                proc.wait(timeout)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(3)
        session, self.helper_session = self.helper_session, None
        if session is not None:
            session.close()
        self._alive = False
