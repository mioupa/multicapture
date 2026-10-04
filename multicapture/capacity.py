"""Automatic limit on the number of simultaneous recordings (requirements §5.6).

The encoder speed (pixels/s) and, for encoders with a per-machine session limit, the number of
sessions are measured once per (PC, encoder, FFmpeg version) and cached in the data directory.
"""
import json
import math
import os
import platform as pyplatform
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime

from . import ffmpeg
from . import platform as osp
from .config import data_dir, work_dir

SAFETY = 0.85
ABS_MAX = 16
NVENC_MAX = 12
MEMORY_RESERVE_GB = 4

MEASURE_STREAMS = 3
MEASURE_WARMUP = 1.0
MEASURE_SECONDS = 4.0
MEASURE_WIDTH, MEASURE_HEIGHT, MEASURE_FPS = 1920, 1080, 30
MEASURE_CLIP_FRAMES = 8
SESSION_PROBE_ENCODERS = {"h264_nvenc": NVENC_MAX, "h264_videotoolbox": ABS_MAX}

SESSION_ERROR_MARKERS = {
    "h264_nvenc": ("OpenEncodeSessionEx", "incompatible client key", "No capable devices found"),
    "h264_videotoolbox": ("-12915", "-12908"),
}

_version_cache = {}


class MeasureCancelled(Exception):
    pass


@dataclass
class Measurement:
    pixels_per_sec: float
    session_cap: int = None
    encoder: str = ""
    pix_fmt: str = ""
    ffmpeg_version: str = ""
    streams: int = MEASURE_STREAMS
    seconds: float = MEASURE_SECONDS
    measured_at: str = ""


def default_pix_fmt():
    return "nv12" if osp.NAME == "mac" else "bgra"


def ffmpeg_version(ffmpeg_path):
    if ffmpeg_path not in _version_cache:
        line = ""
        try:
            out = subprocess.run([ffmpeg_path, "-version"], capture_output=True, text=True, timeout=20,
                                 errors="replace", **osp.popen_kwargs()).stdout
            line = out.splitlines()[0].strip() if out else ""
        except (OSError, subprocess.SubprocessError, IndexError):
            pass
        _version_cache[ffmpeg_path] = line
    return _version_cache[ffmpeg_path]


def cache_key(ffmpeg_path, encoder):
    return f"{pyplatform.node()}|{pyplatform.machine()}|{encoder}|{ffmpeg_version(ffmpeg_path)}"


def _cache_path():
    return os.path.join(data_dir(), "capacity.json")


def _read_cache():
    try:
        with open(_cache_path(), encoding="utf-8") as f:
            raw = json.load(f)
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_cache(raw):
    path = _cache_path()
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass


def load(ffmpeg_path, encoder):
    item = _read_cache().get(cache_key(ffmpeg_path, encoder))
    if not isinstance(item, dict):
        return None
    try:
        known = {k: v for k, v in item.items() if k in Measurement.__dataclass_fields__}
        meas = Measurement(**known)
        return meas if meas.pixels_per_sec > 0 else None
    except (TypeError, ValueError):
        return None


def save(meas, ffmpeg_path=None):
    raw = _read_cache()
    version = meas.ffmpeg_version
    key = f"{pyplatform.node()}|{pyplatform.machine()}|{meas.encoder}|{version}"
    if ffmpeg_path is not None:
        key = cache_key(ffmpeg_path, meas.encoder)
    raw[key] = asdict(meas)
    _write_cache(raw)


def note_session_failure(ffmpeg_path, encoder, running):
    """An encoder session failed to open while `running` recorders were fine: lower the cached cap."""
    meas = load(ffmpeg_path, encoder)
    if meas is None:
        return None
    running = max(1, int(running))
    if meas.session_cap is None or running < meas.session_cap:
        meas.session_cap = running
        save(meas, ffmpeg_path)
    return meas


def log_has_session_error(log_path, encoder):
    markers = SESSION_ERROR_MARKERS.get(encoder)
    if not markers:
        return False
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return False
    return any(m in text for m in markers)


def limit_for(meas, width, height, fps, memory_bytes):
    """(limit, reason): how many simultaneous recordings of this size and fps are sensible."""
    per_stream = max(1, width * height * fps)
    by_speed = math.floor(SAFETY * meas.pixels_per_sec / per_stream)
    by_memory = math.floor(memory_bytes / 2 ** 30 - MEMORY_RESERVE_GB)
    candidates = [(by_speed, "エンコーダの処理能力"), (by_memory, "メモリ")]
    if meas.session_cap:
        candidates.append((meas.session_cap, "エンコーダのセッション数"))
    elif meas.encoder == "h264_nvenc":
        candidates.append((NVENC_MAX, "エンコーダのセッション数"))
    candidates.append((ABS_MAX, f"上限{ABS_MAX}"))
    value, reason = min(candidates, key=lambda c: c[0])
    return max(1, value), reason


def page_load(meas, slots, fps):
    """Required pixel rate of the enabled slots divided by what the encoder can sustain (> 1: too much)."""
    need = sum(s.width * s.height * fps for s in slots)
    return need / (SAFETY * meas.pixels_per_sec)


# ---------------------------------------------------------------- measurement

def _report(on_progress, fraction, text):
    if on_progress:
        on_progress(min(1.0, max(0.0, fraction)), text)


def _check(cancel):
    if cancel is not None and cancel.is_set():
        raise MeasureCancelled()


def _kill(procs):
    for p in procs:
        if p.poll() is None:
            try:
                p.kill()
            except OSError:
                pass
    for p in procs:
        try:
            p.wait(timeout=10)
        except subprocess.SubprocessError:
            pass


def _make_clip(ffmpeg_path, pix_fmt, path):
    cmd = [ffmpeg_path, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", f"testsrc2=size={MEASURE_WIDTH}x{MEASURE_HEIGHT}:rate={MEASURE_FPS}",
           "-frames:v", str(MEASURE_CLIP_FRAMES), "-pix_fmt", pix_fmt, "-f", "rawvideo", path]
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=60, **osp.popen_kwargs())
    if proc.returncode != 0 or not os.path.isfile(path):
        raise RuntimeError("計測用の映像を作れませんでした: " + proc.stderr.strip()[-200:])


def _probe_sessions(ffmpeg_path, encoder, count, on_progress, cancel, base, span):
    encode = ffmpeg.video_encode_args(encoder, 30)
    cmd = [ffmpeg_path, "-hide_banner", "-loglevel", "error", "-y",
           "-re", "-f", "lavfi", "-i", "color=black:s=320x180:r=30", "-t", "3", *encode, "-f", "null", "-"]
    procs = [subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              **osp.popen_kwargs()) for _ in range(count)]
    try:
        deadline = time.monotonic() + 40
        while any(p.poll() is None for p in procs):
            _check(cancel)
            if time.monotonic() > deadline:
                break
            _report(on_progress, base + span * 0.5, f"エンコーダの同時セッション数を調べています（最大{count}本）…")
            time.sleep(0.2)
    finally:
        _kill(procs)
    return sum(1 for p in procs if p.returncode == 0)


class _Stream:
    """One throughput ffmpeg process; samples (monotonic time, frames encoded) from -progress."""

    def __init__(self, cmd):
        self.samples = []
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     **osp.popen_kwargs())
        self.err = []
        self.threads = [threading.Thread(target=self._read_out, daemon=True),
                        threading.Thread(target=self._read_err, daemon=True)]
        for t in self.threads:
            t.start()

    def _read_out(self):
        for raw in self.proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("frame="):
                try:
                    self.samples.append((time.monotonic(), int(line[6:])))
                except ValueError:
                    pass

    def _read_err(self):
        for raw in self.proc.stderr:
            self.err.append(raw.decode("utf-8", "replace").rstrip())
            del self.err[:-20]


def _frames_between(samples, t_from, t_to):
    """(frames, seconds) between the last sample at or before t_from and the last sample at or before t_to."""
    start = None
    end = None
    for t, n in samples:
        if t <= t_from:
            start = (t, n)
        if t <= t_to:
            end = (t, n)
    if start is None and samples:
        start = samples[0]
    if start is None or end is None or end[0] <= start[0]:
        return 0, 0.0
    return end[1] - start[1], end[0] - start[0]


def measure(ffmpeg_path, encoder, pix_fmt, on_progress=None, cancel=None, streams=MEASURE_STREAMS,
            warmup=MEASURE_WARMUP, seconds=MEASURE_SECONDS):
    clip = os.path.join(work_dir(), "capacity_clip.raw")
    session_cap = None
    try:
        _report(on_progress, 0.0, "計測用の映像を準備しています…")
        _check(cancel)
        _make_clip(ffmpeg_path, pix_fmt, clip)

        probe_count = SESSION_PROBE_ENCODERS.get(encoder)
        base = 0.05
        if probe_count:
            _check(cancel)
            session_cap = _probe_sessions(ffmpeg_path, encoder, probe_count, on_progress, cancel, base, 0.25)
            base = 0.3

        encode = ffmpeg.video_encode_args(encoder, MEASURE_FPS, no_bframes=True)
        cmd = [ffmpeg_path, "-hide_banner", "-loglevel", "error", "-y", "-stats_period", "0.25", "-progress", "pipe:1",
               "-stream_loop", "-1", "-f", "rawvideo", "-pix_fmt", pix_fmt,
               "-video_size", f"{MEASURE_WIDTH}x{MEASURE_HEIGHT}", "-framerate", str(MEASURE_FPS), "-i", clip,
               *encode, "-f", "null", "-"]
        procs = []
        try:
            t0 = time.monotonic()
            for _ in range(streams):
                procs.append(_Stream(cmd))
            end = t0 + warmup + seconds
            while time.monotonic() < end:
                _check(cancel)
                dead = [s for s in procs if s.proc.poll() is not None]
                if dead:
                    for t in dead[0].threads:
                        t.join(1)
                    raise RuntimeError("エンコーダの計測に失敗しました: " + " ".join(dead[0].err[-3:])[-300:])
                frac = (time.monotonic() - t0) / (warmup + seconds)
                _report(on_progress, base + (1 - base) * frac,
                        f"エンコーダの処理能力を計測しています（{streams}本を同時にエンコード）…")
                time.sleep(0.1)
            t_end = time.monotonic()
        finally:
            _kill([s.proc for s in procs])
            for s in procs:
                for t in s.threads:
                    t.join(2)

        t_warm = t0 + warmup
        rate = 0.0
        for s in procs:
            n, dt = _frames_between(s.samples, t_warm, t_end)
            if n > 0 and dt > 0:
                rate += n / dt
        if rate <= 0:
            raise RuntimeError("エンコーダの計測に失敗しました（コマが出力されませんでした）")
        pps = rate * MEASURE_WIDTH * MEASURE_HEIGHT
        _report(on_progress, 1.0, "計測が終わりました")
        return Measurement(
            pixels_per_sec=pps, session_cap=session_cap, encoder=encoder, pix_fmt=pix_fmt,
            ffmpeg_version=ffmpeg_version(ffmpeg_path), streams=streams, seconds=seconds,
            measured_at=datetime.now().isoformat(timespec="seconds"),
        )
    finally:
        try:
            os.remove(clip)
        except OSError:
            pass
