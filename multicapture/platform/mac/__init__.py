"""macOS implementation of the interfaces in `multicapture.platform.base`.

Only stdlib + ctypes. Shared libraries are loaded lazily inside functions so that this
module can be imported (e.g. by PyInstaller's analysis on Windows) on any OS.
"""
import ctypes
import os
import platform as _platform
import subprocess
import sys
import tempfile
import threading
import time

__all__ = [
    "NAME", "clock", "thread_init", "popen_kwargs",
    "BROWSER_LABELS", "DEFAULT_BROWSER", "available_browsers", "BrowserWindow",
    "open_window_capture", "open_audio_capture", "AudioPipe", "MUTE_VIA_CAPTURE", "SEQUENTIAL_AUDIO_DELAY", "BROWSER_SETTLE_SECONDS", "WORK_DIR_NAME", "SpeakerMute",
    "restore_speakers_if_needed", "KeepAwake", "DISPLAY_ALWAYS_ON",
    "default_data_dir", "default_output_dir", "FFMPEG_NAME", "ffmpeg_candidates", "open_folder",
    "physical_memory_bytes", "TK_THEME", "UI_FONT", "set_dpi_awareness", "primary_screen_size",
    "check_environment", "SCHEDULE_SUPPORTED", "HW_ENCODERS", "OVERLAP_HINT",
    "check_permissions", "request_permissions", "permissions_ok", "cleanup_leftovers", "capture_hint",
]

NAME = "mac"
DEFAULT_BROWSER = "chrome"
MUTE_VIA_CAPTURE = True
# The helper already shifts tap timestamps by the output latency (PROTOCOL.md).
SEQUENTIAL_AUDIO_DELAY = 0.0
# Freshly started browsers drop video frames for ~30 s (Spotlight, fonts, model loading, ...).
# ".noindex" keeps Spotlight from indexing the cloned profiles.
BROWSER_SETTLE_SECONDS = 30
WORK_DIR_NAME = "work.noindex"
DISPLAY_ALWAYS_ON = True
FFMPEG_NAME = "ffmpeg"
TK_THEME = "aqua"
UI_FONT = "Hiragino Sans"
HW_ENCODERS = ["h264_videotoolbox"]
OVERLAP_HINT = "録画中のウィンドウは一部が見えていればOK（完全に隠す・最小化は不可）"
SCHEDULE_SUPPORTED = False
STALL_HINT = "録画用ウィンドウが隠れているか最小化されています。一部でも見える状態にしてください"

BROWSER_LABELS = {"chrome": "Google Chrome", "edge": "Microsoft Edge"}

_BROWSER_APPS = {
    "chrome": ("Google Chrome.app", "Google Chrome"),
    "edge": ("Microsoft Edge.app", "Microsoft Edge"),
}


class _MachClock:
    """mach_absolute_time in seconds (the host time base of ScreenCaptureKit / Core Audio)."""

    def __init__(self):
        self._lib = None
        self._scale = None

    def _load(self):
        class MachTimebaseInfo(ctypes.Structure):
            _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]

        lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        lib.mach_absolute_time.restype = ctypes.c_uint64
        lib.mach_absolute_time.argtypes = []
        info = MachTimebaseInfo()
        lib.mach_timebase_info(ctypes.byref(info))
        self._scale = info.numer / info.denom / 1e9
        self._lib = lib

    def now(self):
        if self._lib is None:
            self._load()
        return self._lib.mach_absolute_time() * self._scale


clock = _MachClock()


def thread_init():
    pass


def popen_kwargs():
    return {}


def available_browsers():
    found = {}
    roots = ["/Applications", os.path.expanduser("~/Applications")]
    for kind, (app, binary) in _BROWSER_APPS.items():
        for root in roots:
            exe = os.path.join(root, app, "Contents", "MacOS", binary)
            if os.path.isfile(exe):
                found[kind] = exe
                break
    return found


from .browser import BrowserWindow  # noqa: E402
from .capture import open_audio_capture, open_window_capture  # noqa: E402
from . import helper as _helper  # noqa: E402


def check_permissions():
    """{"screen": bool, "audio": "granted|denied|unknown"} from the helper; raises HelperError if it cannot run."""
    return _helper.check_permission()


def request_permissions():
    """Ask for the missing permissions (shows the system dialogs); returns the same dict as check_permissions()."""
    return _helper.request_permission()


def permissions_ok(perms):
    return bool(perms) and bool(perms.get("screen")) and perms.get("audio") == "granted"


def capture_hint(fps):
    """Message when a recording's video capture has stalled or runs far below `fps` (window hidden/minimized)."""
    now = time.monotonic()
    for session in _helper.active_sessions():
        if not session.video_active or session.video_started_at is None or now - session.video_started_at < 5:
            continue
        if session.stalled:
            return STALL_HINT
        recent = [st for t, st in session.stats_history if now - t <= 3.5][-3:]
        if len(recent) >= 3 and all(
                st.get("complete", 0) + st.get("idle", 0) + st.get("other", 0) < 0.8 * fps for st in recent):
            return STALL_HINT
    return None


def _process_table():
    out = subprocess.run(["ps", "-axo", "pid=,ppid=,command="], capture_output=True, text=True).stdout
    rows = []
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            rows.append((int(parts[0]), int(parts[1]), parts[2]))
    return rows


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def cleanup_leftovers(profile_root, rows=None, kill=None):
    """Kill processes a crashed earlier run left behind (§8): browsers whose --user-data-dir is under
    `profile_root`, orphaned mc-capture helpers started from our own paths, and our `caffeinate -w <pid>`
    whose pid is gone. Returns the list of killed pids. `rows`/`kill` are injectable for tests."""
    import re
    import signal

    roots = {os.path.abspath(profile_root), os.path.realpath(profile_root)}
    roots |= {r.replace(os.sep + "private", "", 1) for r in list(roots) if r.startswith(os.sep + "private")}
    prefixes = tuple(f"--user-data-dir={r.rstrip(os.sep)}{os.sep}" for r in roots)
    helpers = tuple(_helper.helper_executables())
    me = os.getpid()
    ancestors = {me, os.getppid()}
    victims = []
    for pid, ppid, cmd in (rows if rows is not None else _process_table()):
        if pid in ancestors:
            continue
        if any(p in cmd for p in prefixes) or any(cmd.endswith("--user-data-dir=" + r.rstrip(os.sep)) for r in roots):
            victims.append(pid)
        elif ppid == 1 and any(cmd.startswith(h + " ") for h in helpers) and re.search(r"\sserve$", cmd):
            victims.append(pid)
        else:
            m = re.match(r"^(?:/usr/bin/)?caffeinate -d -i -w (\d+)$", cmd)
            if m and not _pid_alive(int(m.group(1))):
                victims.append(pid)
    if not victims:
        return []
    kill = kill or os.kill
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in victims:
            try:
                kill(pid, sig)
            except OSError:
                pass
        if sig == signal.SIGTERM and kill is os.kill:
            time.sleep(1.0)
    return victims


class AudioPipe:
    """FIFO FFmpeg reads audio from, in a private (0700) temp dir; the FIFO itself is 0600."""

    def __init__(self):
        self._dir = tempfile.mkdtemp(prefix="multicapture-")
        self.path = os.path.join(self._dir, "audio.fifo")
        try:
            os.mkfifo(self.path, 0o600)
        except OSError:
            os.rmdir(self._dir)
            raise
        self._fd = None
        self._closed = False
        self._lock = threading.Lock()

    def connect(self):
        # Blocks until the reader (FFmpeg) opens the FIFO; close() unblocks it.
        fd = os.open(self.path, os.O_WRONLY)
        with self._lock:
            if self._closed:
                os.close(fd)
                raise OSError("audio pipe closed")
            self._fd = fd

    def write(self, data):
        fd = self._fd
        if fd is None:
            raise BrokenPipeError("audio pipe not connected")
        view = memoryview(data)
        offset = 0
        while offset < len(view):
            try:
                offset += os.write(fd, view[offset:])
            except (InterruptedError, BlockingIOError):
                continue
            except OSError as exc:
                if exc.errno == 32:
                    raise BrokenPipeError("audio pipe closed") from exc
                raise

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            fd, self._fd = self._fd, None
        if fd is None:
            # A connect() may still be waiting for a reader: become one briefly.
            try:
                os.close(os.open(self.path, os.O_RDONLY | os.O_NONBLOCK))
            except OSError:
                pass
        else:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.remove(self.path)
        except OSError:
            pass
        try:
            os.rmdir(self._dir)
        except OSError:
            pass


class SpeakerMute:
    """No-op: muting is done by open_audio_capture(mute=True) on macOS."""

    def __init__(self, state_path):
        self.state_path = state_path
        self.active = False

    def mute(self):
        pass

    def restore(self):
        pass


def restore_speakers_if_needed(state_path):
    pass


class KeepAwake:
    """Keeps the display and system awake via `caffeinate`, which exits with this process."""

    def __init__(self, reason):
        self._reason = reason
        self._proc = None

    def acquire(self, keep_display=True):
        if self._proc is not None and self._proc.poll() is None:
            return True
        try:
            self._proc = subprocess.Popen(
                ["caffeinate", "-d", "-i", "-w", str(os.getpid())],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError:
            self._proc = None
            return False
        return True

    def release(self):
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except (OSError, subprocess.SubprocessError):
            pass


def default_data_dir(app_dir):
    path = os.path.expanduser("~/Library/Application Support/MultiCapture")
    os.makedirs(path, exist_ok=True)
    return path


def default_output_dir():
    return os.path.join(os.path.expanduser("~"), "Movies", "MultiCapture")


def ffmpeg_candidates(app_dir):
    paths = []
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        paths.append(os.path.join(bundle, "ffmpeg"))
    if getattr(sys, "frozen", False):
        contents = os.path.dirname(os.path.dirname(os.path.abspath(sys.executable)))
        paths.append(os.path.join(contents, "Frameworks", "ffmpeg"))
        paths.append(os.path.join(contents, "Resources", "ffmpeg"))
    paths += [
        os.path.join(app_dir, "ffmpeg", "ffmpeg"),
        os.path.join(app_dir, "ffmpeg"),
        "/opt/homebrew/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
    ]
    return paths


def open_folder(path):
    subprocess.Popen(["open", path])


def physical_memory_bytes():
    return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")


def set_dpi_awareness():
    pass


def primary_screen_size():
    class CGPoint(ctypes.Structure):
        _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]

    class CGSize(ctypes.Structure):
        _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]

    class CGRect(ctypes.Structure):
        _fields_ = [("origin", CGPoint), ("size", CGSize)]

    lib = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    lib.CGMainDisplayID.restype = ctypes.c_uint32
    lib.CGMainDisplayID.argtypes = []
    lib.CGDisplayBounds.restype = CGRect
    lib.CGDisplayBounds.argtypes = [ctypes.c_uint32]
    rect = lib.CGDisplayBounds(lib.CGMainDisplayID())
    return int(rect.size.width), int(rect.size.height)


def _macos_major():
    version = _platform.mac_ver()[0]
    if version.startswith("10.16"):
        # Python built against an old SDK reports 10.16 for macOS 11+ (compat mode).
        try:
            version = subprocess.run(
                ["sw_vers", "-productVersion"], capture_output=True, text=True, timeout=5,
                env={**os.environ, "SYSTEM_VERSION_COMPAT": "0"},
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        return int(version.split(".")[0])
    except ValueError:
        return 0


def check_environment():
    if _macos_major() < 15 or _platform.machine() != "arm64":
        return "このアプリはmacOS 15以降のApple Silicon搭載Macが必要です。"
    return None
