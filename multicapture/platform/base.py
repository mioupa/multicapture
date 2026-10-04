"""OS-dependent interfaces.

`multicapture.platform` selects `windows` or `mac` by `sys.platform` and re-exports the
names below from it. Everything outside `multicapture/platform/` talks to the OS only
through these names, so `recorder.py` and `splitjob.py` stay OS-agnostic.

Module-level names every implementation exports:

    NAME                 "windows" or "mac"
    clock                Clock instance (same time base as capture timestamps)
    thread_init()        per-thread setup before touching capture APIs (COM MTA on Windows)
    popen_kwargs()       extra subprocess.Popen kwargs that hide console windows ({} on mac)

    BROWSER_LABELS       {kind: label}
    DEFAULT_BROWSER      "edge" on Windows, "chrome" on mac
    available_browsers() {kind: executable path} of installed browsers
    BrowserWindow        see BrowserWindow below

    open_window_capture(browser_window) -> WindowCapture
    open_audio_capture(browser_window, sample_rate, channels, mute=False) -> AudioCapture
    AudioPipe            see AudioPipe below
    MUTE_VIA_CAPTURE     True when muting is done by open_audio_capture(mute=True) (mac)
    SpeakerMute          see SpeakerMute below
    restore_speakers_if_needed(state_path)

    KeepAwake            see KeepAwake below
    DISPLAY_ALWAYS_ON    True when the display is always kept on while recording (mac)

    default_data_dir(app_dir) -> str
    default_output_dir() -> str
    FFMPEG_NAME          "ffmpeg.exe" or "ffmpeg"
    ffmpeg_candidates(app_dir) -> list[str]   searched in order before PATH
    open_folder(path)
    physical_memory_bytes() -> int
    HW_ENCODERS          hardware H264 encoders probed in order by ffmpeg.detect_encoder
                         (["h264_nvenc", "h264_amf", "h264_qsv"] on Windows, ["h264_videotoolbox"] on mac)
    OVERLAP_HINT         UI text about recording windows being covered or minimized

    TK_THEME, UI_FONT    ttk theme name and UI font family
    set_dpi_awareness()
    primary_screen_size() -> (width, height)
    check_environment() -> str | None   error message when this OS/hardware is unsupported
    SCHEDULE_SUPPORTED   whether --start/--duration scheduled recording is available
"""


class Clock:
    def now(self):
        """Seconds on the same time base as WindowCapture and AudioCapture timestamps."""
        raise NotImplementedError


class BrowserWindow:
    """A browser instance with its own profile, launched for recording or login."""

    pid = None

    def __init__(self, exe_path, profile_dir, url, x, y, width, height, app=True, debug=False):
        raise NotImplementedError

    def launch(self, timeout=30.0):
        raise NotImplementedError

    def devtools_url(self, timeout=20.0):
        raise NotImplementedError

    def alive(self):
        raise NotImplementedError

    def fit_content(self, width, height, attempts=3):
        """Resize so the page area is width x height; return the size actually reached."""
        raise NotImplementedError

    def capture_offset(self):
        """(x, y) of the page area inside the captured window frame, or None if unknown."""
        raise NotImplementedError

    def restore_if_minimized(self):
        raise NotImplementedError

    def close(self, timeout=8.0):
        raise NotImplementedError


class WindowCapture:
    """Captures one browser window into a ring of RING_SIZE frames.

    Frames are written to FFmpeg as raw video in `pix_fmt` ("bgra" or "nv12"),
    tightly packed at the size given to set_output().
    """

    pix_fmt = "bgra"
    seq = 0           # sequence number of the newest frame in the ring
    head = 0          # ring slot of the newest frame
    ring_times = ()   # capture time (Clock seconds) per ring slot
    ring_seq = ()     # sequence number per ring slot

    def start(self):
        raise NotImplementedError

    def set_output(self, width, height):
        raise NotImplementedError

    def set_region(self, left, top):
        raise NotImplementedError

    def update(self):
        """Pull new frames into the ring; True when a new frame arrived."""
        raise NotImplementedError

    def slot_at(self, wall):
        raise NotImplementedError

    def first_seq_after(self, wall):
        raise NotImplementedError

    def oldest_seq(self):
        raise NotImplementedError

    def slot_of_seq(self, seq):
        raise NotImplementedError

    def write_slot(self, sink, slot):
        """Write the frame in `slot` by calling sink(buffer)."""
        raise NotImplementedError

    def close(self):
        raise NotImplementedError


class AudioCapture:
    """Audio of one browser (its process tree), s16le interleaved."""

    def start(self, timeout=5.0):
        raise NotImplementedError

    def read_timed(self):
        """[(bytes, timestamp in Clock seconds of the first sample)] captured since the last call."""
        raise NotImplementedError

    def close(self):
        raise NotImplementedError


class AudioPipe:
    """Path FFmpeg opens as its audio input; we write s16le into it."""

    path = ""

    def connect(self):
        """Block until FFmpeg has opened the path."""
        raise NotImplementedError

    def write(self, data):
        raise NotImplementedError

    def close(self):
        raise NotImplementedError


class SpeakerMute:
    """Silences the speakers during recording and restores them afterwards.

    Where MUTE_VIA_CAPTURE is True this is a no-op and muting happens in
    open_audio_capture(mute=True) instead.
    """

    active = False

    def __init__(self, state_path):
        raise NotImplementedError

    def mute(self):
        raise NotImplementedError

    def restore(self):
        raise NotImplementedError


class KeepAwake:
    def __init__(self, reason):
        raise NotImplementedError

    def acquire(self, keep_display=True):
        raise NotImplementedError

    def release(self):
        raise NotImplementedError
