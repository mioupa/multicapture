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

__all__ = [
    "NAME", "clock", "thread_init", "popen_kwargs",
    "BROWSER_LABELS", "DEFAULT_BROWSER", "available_browsers", "BrowserWindow",
    "open_window_capture", "open_audio_capture", "AudioPipe", "MUTE_VIA_CAPTURE", "SpeakerMute",
    "restore_speakers_if_needed", "KeepAwake", "DISPLAY_ALWAYS_ON",
    "default_data_dir", "default_output_dir", "FFMPEG_NAME", "ffmpeg_candidates", "open_folder",
    "physical_memory_bytes", "TK_THEME", "UI_FONT", "set_dpi_awareness", "primary_screen_size",
    "check_environment", "SCHEDULE_SUPPORTED", "HW_ENCODERS", "OVERLAP_HINT",
]

NAME = "mac"
DEFAULT_BROWSER = "chrome"
MUTE_VIA_CAPTURE = True
DISPLAY_ALWAYS_ON = True
FFMPEG_NAME = "ffmpeg"
TK_THEME = "aqua"
UI_FONT = "Hiragino Sans"
HW_ENCODERS = ["h264_videotoolbox"]
OVERLAP_HINT = "録画中のウィンドウは一部が見えていればOK（完全に隠す・最小化は不可）"
SCHEDULE_SUPPORTED = False

BROWSER_LABELS = {"chrome": "Google Chrome", "edge": "Microsoft Edge"}

_BROWSER_APPS = {
    "chrome": ("Google Chrome.app", "Google Chrome"),
    "edge": ("Microsoft Edge.app", "Microsoft Edge"),
}

_NOT_YET = "macOS capture is implemented in Phase 2"


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


class BrowserWindow:
    def __init__(self, *args, **kwargs):
        raise NotImplementedError(_NOT_YET)


def open_window_capture(browser_window):
    raise NotImplementedError(_NOT_YET)


def open_audio_capture(browser_window, sample_rate, channels, mute=False):
    raise NotImplementedError(_NOT_YET)


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
