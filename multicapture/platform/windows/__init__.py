"""Windows implementation of the interfaces in `multicapture.platform.base`."""
import os
import sys
import time

from . import audiosession, loopback, win32, wgc
from .audiosession import SpeakerMute
from .browser import BROWSER_LABELS, CREATE_NO_WINDOW, BrowserWindow, available_browsers
from .pipe import AudioPipe
from .win32 import KeepAwake, primary_screen_size, set_dpi_awareness

__all__ = [
    "NAME", "clock", "thread_init", "popen_kwargs",
    "BROWSER_LABELS", "DEFAULT_BROWSER", "available_browsers", "BrowserWindow",
    "open_window_capture", "open_audio_capture", "AudioPipe", "MUTE_VIA_CAPTURE", "SEQUENTIAL_AUDIO_DELAY", "BROWSER_SETTLE_SECONDS", "WORK_DIR_NAME", "SpeakerMute",
    "restore_speakers_if_needed", "KeepAwake", "DISPLAY_ALWAYS_ON",
    "default_data_dir", "default_output_dir", "FFMPEG_NAME", "ffmpeg_candidates", "open_folder",
    "physical_memory_bytes", "TK_THEME", "UI_FONT", "set_dpi_awareness", "primary_screen_size",
    "check_environment", "SCHEDULE_SUPPORTED", "HW_ENCODERS", "OVERLAP_HINT",
]

NAME = "windows"
DEFAULT_BROWSER = "edge"
MUTE_VIA_CAPTURE = False
SEQUENTIAL_AUDIO_DELAY = -0.025
BROWSER_SETTLE_SECONDS = 0
WORK_DIR_NAME = "work"
DISPLAY_ALWAYS_ON = False
FFMPEG_NAME = "ffmpeg.exe"
TK_THEME = "vista"
UI_FONT = "Yu Gothic UI"
HW_ENCODERS = ["h264_nvenc", "h264_amf", "h264_qsv"]
OVERLAP_HINT = "録画中のウィンドウは重なっていても裏に隠れていてもOK（最小化のみ不可）"
SCHEDULE_SUPPORTED = True


class _Clock:
    now = staticmethod(time.perf_counter)


clock = _Clock()
thread_init = win32.ensure_mta


def popen_kwargs():
    return {"creationflags": CREATE_NO_WINDOW}


class _WindowCapture(wgc.WindowCapture):
    """wgc.WindowCapture that owns its D3D device (closed after the capture)."""

    pix_fmt = "bgra"

    def __init__(self, hwnd, device):
        super().__init__(hwnd, device)
        self._owned_device = device

    def close(self):
        try:
            super().close()
        finally:
            self._owned_device.close()


def open_window_capture(browser_window):
    device = wgc.D3DDevice()
    try:
        return _WindowCapture(browser_window.hwnd, device)
    except BaseException:
        device.close()
        raise


def open_audio_capture(browser_window, sample_rate, channels, mute=False):
    return loopback.ProcessLoopback(browser_window.pid, sample_rate, channels)


def restore_speakers_if_needed(state_path):
    if os.path.exists(state_path):
        try:
            SpeakerMute(state_path).restore()
        except OSError:
            pass


def _writable(path):
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".write_test")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        return True
    except OSError:
        return False


def default_data_dir(app_dir):
    portable = os.path.join(app_dir, "data")
    if _writable(portable):
        return portable
    fallback = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "MultiCapture")
    os.makedirs(fallback, exist_ok=True)
    return fallback


def default_output_dir():
    return os.path.join(os.path.expanduser("~"), "Videos", "MultiCapture")


def ffmpeg_candidates(app_dir):
    return [
        os.path.join(app_dir, "ffmpeg", "ffmpeg.exe"),
        os.path.join(app_dir, "ffmpeg.exe"),
        os.path.join(app_dir, "_internal", "ffmpeg.exe"),
    ]


def open_folder(path):
    os.startfile(path)


def physical_memory_bytes():
    import ctypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise ctypes.WinError()
    return int(status.ullTotalPhys)


def check_environment():
    if sys.getwindowsversion().build < 22000:
        return "このアプリはWindows 11以降が必要です。\n(ブラウザごとの音声取得機能がWindows 10にはありません)"
    return None
