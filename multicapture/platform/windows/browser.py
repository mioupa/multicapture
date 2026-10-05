import os
import subprocess
import time
import winreg

from ...browser_common import RENDER_FLAGS
from . import win32

CREATE_NO_WINDOW = 0x08000000

BROWSER_EXES = {
    "edge": ("msedge.exe", [
        r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
        r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
        r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe",
    ]),
    "chrome": ("chrome.exe", [
        r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
        r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
        r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
    ]),
}

BROWSER_LABELS = {"edge": "Microsoft Edge", "chrome": "Google Chrome"}


def _from_app_paths(exe_name):
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}") as key:
                value, _ = winreg.QueryValueEx(key, None)
                if value and os.path.isfile(value.strip('"')):
                    return value.strip('"')
        except OSError:
            pass
    return None


def find_browser(kind):
    exe_name, candidates = BROWSER_EXES[kind]
    path = _from_app_paths(exe_name)
    if path:
        return path
    for candidate in candidates:
        expanded = os.path.expandvars(candidate)
        if os.path.isfile(expanded):
            return expanded
    return None


def available_browsers():
    return {kind: path for kind in BROWSER_EXES if (path := find_browser(kind))}


def _browser_pid_for_profile(exe_path, profile_dir):
    exe_name = os.path.basename(exe_path)
    needle = profile_dir.replace("'", "''")
    script = (
        f"Get-CimInstance Win32_Process -Filter \"Name='{exe_name}'\" | "
        f"Where-Object {{ $_.CommandLine -like '*{needle}*' -and $_.CommandLine -notlike '*--type=*' }} | "
        "Select-Object -First 1 -ExpandProperty ProcessId"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=20, creationflags=CREATE_NO_WINDOW,
        ).stdout.strip()
        return int(out) if out else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


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
        self.debug = debug
        self.process = None
        self.pid = None
        self.hwnd = None

    def devtools_url(self, timeout=20.0):
        from ...cdp import read_devtools_port
        return read_devtools_port(self.profile_dir, timeout)

    def launch(self, timeout=30.0):
        os.makedirs(self.profile_dir, exist_ok=True)
        if self.debug:
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
            *(["--remote-debugging-port=0"] if self.debug else []),
            f"--app={self.url}" if self.app else self.url,
        ]
        self.process = subprocess.Popen(args, creationflags=CREATE_NO_WINDOW)
        self.pid = self.process.pid
        deadline = time.monotonic() + timeout
        handed_off = False
        while time.monotonic() < deadline:
            if not handed_off and self.process.poll() is not None:
                handed_off = True
                self.pid = _browser_pid_for_profile(self.exe_path, self.profile_dir)
                if self.pid is None:
                    raise RuntimeError("ブラウザの起動に失敗しました")
            self.hwnd = self._find_window()
            if self.hwnd:
                return
            time.sleep(0.25)
        raise RuntimeError("ブラウザのウィンドウが見つかりませんでした")

    def _find_window(self):
        for hwnd in win32.top_level_windows():
            if win32.window_pid(hwnd) != self.pid:
                continue
            if win32.class_name(hwnd) != "Chrome_WidgetWin_1" or not win32.user32.IsWindowVisible(hwnd):
                continue
            if self._render_widget(hwnd):
                return hwnd
        return None

    @staticmethod
    def _render_widget(hwnd):
        best, best_area = None, 0
        for child in win32.child_windows(hwnd):
            if win32.class_name(child) != "Chrome_RenderWidgetHostHWND" or not win32.user32.IsWindowVisible(child):
                continue
            l, t, r, b = win32.window_rect(child)
            area = (r - l) * (b - t)
            if area > best_area:
                best, best_area = child, area
        return best

    def alive(self):
        return self.hwnd is not None and win32.is_window(self.hwnd)

    def restore_if_minimized(self):
        win32.restore_if_minimized(self.hwnd)

    def content_rect(self):
        widget = self._render_widget(self.hwnd)
        if widget is None:
            return None
        return win32.window_rect(widget)

    def fit_content(self, width, height, attempts=3):
        for _ in range(attempts):
            win32.restore_if_minimized(self.hwnd)
            content = self.content_rect()
            if content is None:
                time.sleep(0.3)
                continue
            cw, ch = content[2] - content[0], content[3] - content[1]
            if (cw, ch) == (width, height):
                return cw, ch
            wl, wt, wr, wb = win32.window_rect(self.hwnd)
            win32.move_window(self.hwnd, self.x, self.y, (wr - wl) + width - cw, (wb - wt) + height - ch)
            time.sleep(0.4)
        content = self.content_rect()
        if content is None:
            raise RuntimeError("ページ表示領域を特定できませんでした")
        return content[2] - content[0], content[3] - content[1]

    def capture_offset(self):
        content = self.content_rect()
        if content is None:
            return None
        fl, ft, _, _ = win32.frame_bounds(self.hwnd)
        return content[0] - fl, content[1] - ft

    def close(self, timeout=8.0):
        if self.hwnd and win32.is_window(self.hwnd):
            win32.close_window(self.hwnd)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and self.alive():
            time.sleep(0.2)
        if self.process and self.process.poll() is None and self.alive():
            self.process.terminate()
        self.hwnd = None
