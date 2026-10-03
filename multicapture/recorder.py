import ctypes
import math
import os
import queue
import subprocess
import threading
import time
import uuid
from ctypes import wintypes
from datetime import datetime

from . import ffmpeg, win32
from .config import log_dir
from .loopback import ProcessLoopback
from .wgc import D3DDevice, WindowCapture

kernel32 = win32.kernel32
kernel32.CreateNamedPipeW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
]
kernel32.CreateNamedPipeW.restype = wintypes.HANDLE
kernel32.ConnectNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
kernel32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
kernel32.DisconnectNamedPipe.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

PIPE_ACCESS_OUTBOUND = 0x00000002
PIPE_TYPE_BYTE = 0x00000000
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
ERROR_PIPE_CONNECTED = 535

SAMPLE_RATE = 48000
CHANNELS = 2
BLOCK_ALIGN = CHANNELS * 2
AUDIO_MAX_LAG = 0.15
AUDIO_TARGET_LAG = 0.04
AUDIO_MAX_LEAD = 0.1
AUDIO_QUEUE_CHUNKS = 3000
AUDIO_TAIL_SECONDS = 0.3
AUDIO_DELAY_SECONDS = 0.0
POLL_INTERVAL = 0.004
PHASE_SMOOTHING = 0.98
PHASE_LOCK_THRESHOLD = 0.3
MAX_CATCH_UP_SECONDS = 15


class AudioPipe:
    def __init__(self):
        self.path = rf"\\.\pipe\multicapture-{uuid.uuid4().hex}"
        self.handle = kernel32.CreateNamedPipeW(self.path, PIPE_ACCESS_OUTBOUND, PIPE_TYPE_BYTE, 1, 1 << 20, 0, 0, None)
        if self.handle == INVALID_HANDLE_VALUE:
            raise OSError("名前付きパイプを作成できませんでした")

        self.connected = False

    def connect(self):
        if not kernel32.ConnectNamedPipe(self.handle, None) and ctypes.get_last_error() != ERROR_PIPE_CONNECTED:
            raise OSError("FFmpegが音声パイプに接続しませんでした")
        self.connected = True

    def write(self, data):
        base = ctypes.cast(ctypes.c_char_p(data), ctypes.c_void_p).value
        offset = 0
        while offset < len(data):
            written = wintypes.DWORD()
            if not kernel32.WriteFile(self.handle, base + offset, len(data) - offset, ctypes.byref(written), None):
                raise BrokenPipeError("audio pipe closed")
            offset += written.value

    def close(self):
        if self.handle and not self.connected:
            try:
                open(self.path, "rb").close()
            except OSError:
                pass
        if self.handle and self.handle != INVALID_HANDLE_VALUE:
            kernel32.FlushFileBuffers(self.handle)
            kernel32.CloseHandle(self.handle)
            self.handle = None


def even(n):
    return max(2, n - (n % 2))


class SlotRecorder:
    def __init__(self, index, slot, browser_window, settings, ffmpeg_path, encoder, on_status,
                 output_path=None, trim_start=0.0, log_name=None):
        self.index = index
        self.slot = slot
        self.browser = browser_window
        self.settings = settings
        self.ffmpeg_path = ffmpeg_path
        self.encoder = encoder
        self.on_status = on_status
        self.stop_event = threading.Event()
        self.output_path = output_path
        self.trim_start = trim_start
        self.log_name = log_name or f"slot{index + 1}_ffmpeg.log"
        self.error = None
        self.frames_written = 0
        self.new_frames = 0
        self.ready = threading.Event()
        self.video_done = threading.Event()
        self._thread = None
        self._clock_lock = threading.Lock()
        self._t0 = None
        self._pause_total = 0.0
        self._pause_started = None

    def elapsed(self):
        with self._clock_lock:
            if self._t0 is None:
                return 0.0
            now = time.perf_counter()
            paused = (now - self._pause_started) if self._pause_started is not None else 0.0
            return now - self._t0 - self._pause_total - paused

    @property
    def paused(self):
        return self._pause_started is not None

    def pause(self):
        with self._clock_lock:
            if self._pause_started is None:
                self._pause_started = time.perf_counter()

    def resume(self):
        with self._clock_lock:
            if self._pause_started is not None:
                self._pause_total += time.perf_counter() - self._pause_started
                self._pause_started = None

    def status(self, text):
        self.on_status(self.index, text)

    def start(self):
        self._thread = threading.Thread(target=self._run, name=f"slot{self.index + 1}", daemon=True)
        self._thread.start()

    def stop(self):
        self.stop_event.set()

    def join(self, timeout=None):
        if self._thread:
            self._thread.join(timeout)

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def _run(self):
        device = capture = loop = pipe = proc = None
        log = None
        audio_thread = None
        try:
            win32.ensure_mta()
            fps = self.settings.fps
            out_w, out_h = even(self.slot.width), even(self.slot.height)
            cw, ch = self.browser.fit_content(out_w, out_h)
            cap_w, cap_h = even(min(cw, out_w)), even(min(ch, out_h))

            device = D3DDevice()
            capture = WindowCapture(self.browser.hwnd, device)
            capture.start()
            capture.set_output(cap_w, cap_h)
            offset = self.browser.capture_offset() or (0, 0)
            capture.set_region(*offset)

            loop = ProcessLoopback(self.browser.pid, SAMPLE_RATE, CHANNELS)
            loop.start()

            if not self.output_path:
                os.makedirs(self.settings.output_dir, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in self.slot.name) or f"slot{self.index + 1}"
                self.output_path = os.path.join(self.settings.output_dir, f"{safe}_{stamp}.mp4")

            pipe = AudioPipe()
            cmd = ffmpeg.build_command(
                self.ffmpeg_path, cap_w, cap_h, fps, pipe.path, SAMPLE_RATE, CHANNELS,
                out_w, out_h, self.encoder, self.output_path, trim_start=self.trim_start,
                no_bframes=self.trim_start > 0,
            )
            log = open(os.path.join(log_dir(), self.log_name), "w", encoding="utf-8", errors="replace")
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log,
                creationflags=ffmpeg.CREATE_NO_WINDOW,
            )

            with self._clock_lock:
                self._t0 = time.perf_counter()
            self.ready.set()
            audio_thread = threading.Thread(target=self._audio_loop, args=(loop, pipe), daemon=True)
            audio_thread.start()

            label = f"録画中 {cap_w}x{cap_h}" + ("" if (cap_w, cap_h) == (out_w, out_h) else f" → {out_w}x{out_h}")
            self.status(label)
            try:
                self._video_loop(capture, proc, fps, label)
            finally:
                self.video_done.set()
        except Exception as exc:
            self.error = str(exc)
            self.status(f"エラー: {exc}")
        finally:
            self.stop_event.set()
            self.video_done.set()
            if proc and not self.error:
                self.status("保存中…")
            if audio_thread:
                audio_thread.join(5)
            if proc:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            if pipe:
                pipe.close()
            if proc:
                try:
                    proc.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    proc.kill()
                if proc.returncode not in (0, None) and not self.error:
                    self.error = f"FFmpegが異常終了しました (code {proc.returncode})"
                    self.status(f"エラー: {self.error}")
            if log:
                log.close()
            for obj in (loop, capture, device):
                if obj:
                    try:
                        obj.close()
                    except Exception:
                        pass
            if not self.error:
                self.status("保存完了" if self.output_path else "停止")

    def _video_loop(self, capture, proc, fps, label):
        write = proc.stdin.write
        last_geometry = time.perf_counter()
        last_report = last_geometry
        period = 1.0 / fps
        phase_x = phase_y = 0.0
        while not self.stop_event.is_set():
            now = time.perf_counter()
            if now - last_geometry > 1.0:
                last_geometry = now
                if not self.browser.alive():
                    raise RuntimeError("ブラウザが閉じられました")
                win32.restore_if_minimized(self.browser.hwnd)
                offset = self.browser.capture_offset()
                if offset:
                    capture.set_region(*offset)
            if capture.update():
                self.new_frames += 1
                angle = 2 * math.pi * ((capture.last_frame_time % period) / period)
                phase_x = PHASE_SMOOTHING * phase_x + (1 - PHASE_SMOOTHING) * math.cos(angle)
                phase_y = PHASE_SMOOTHING * phase_y + (1 - PHASE_SMOOTHING) * math.sin(angle)
            if self.paused:
                time.sleep(POLL_INTERVAL)
                continue
            due = int(self.elapsed() * fps) + 1
            behind = due - self.frames_written
            if behind > fps * MAX_CATCH_UP_SECONDS:
                self.frames_written = due - fps
                behind = fps
            if behind > 0:
                base = time.perf_counter() - self.elapsed()
                locked = math.hypot(phase_x, phase_y) > PHASE_LOCK_THRESHOLD
                if locked:
                    arrival = (math.atan2(phase_y, phase_x) / (2 * math.pi) % 1.0) * period
                    target = (arrival + period / 2) % period
            for _ in range(max(0, behind)):
                if self.stop_event.is_set():
                    break
                nominal = base + self.frames_written / fps
                sample_time = nominal - ((nominal - target) % period) if locked else nominal
                capture.write_to(write, sample_time)
                self.frames_written += 1
            if now - last_report > 1.0:
                last_report = now
                elapsed = int(self.elapsed())
                self.status(f"{label}  {elapsed // 3600:d}:{elapsed // 60 % 60:02d}:{elapsed % 60:02d}")
            sleep = self.frames_written / fps - self.elapsed()
            if sleep > 0:
                time.sleep(min(sleep, POLL_INTERVAL))

    def _audio_loop(self, loop, pipe):
        q = queue.Queue(maxsize=AUDIO_QUEUE_CHUNKS)
        failure = []

        def writer():
            try:
                pipe.connect()
                while True:
                    chunk = q.get()
                    if chunk is None:
                        return
                    pipe.write(chunk)
            except OSError as exc:
                failure.append(exc)
                self.stop_event.set()

        wt = threading.Thread(target=writer, daemon=True)
        wt.start()
        win32.ensure_mta()
        loop.read()
        delay = int(AUDIO_DELAY_SECONDS * SAMPLE_RATE)
        q.put(bytes(delay * BLOCK_ALIGN))
        written = delay
        try:
            while not self.stop_event.is_set():
                data = loop.read()
                if self.paused:
                    time.sleep(0.01)
                    continue
                expected = int(self.elapsed() * SAMPLE_RATE)
                frames = len(data) // BLOCK_ALIGN
                if written + frames < expected - AUDIO_MAX_LAG * SAMPLE_RATE:
                    pad = expected - int(AUDIO_TARGET_LAG * SAMPLE_RATE) - (written + frames)
                    if pad > 0:
                        data = bytes(pad * BLOCK_ALIGN) + data
                        frames += pad
                elif written + frames > expected + AUDIO_MAX_LEAD * SAMPLE_RATE:
                    drop = min(frames, written + frames - expected)
                    data = data[drop * BLOCK_ALIGN:]
                    frames -= drop
                if data:
                    try:
                        q.put_nowait(data)
                    except queue.Full:
                        pass
                    written += frames
                time.sleep(0.01)
        except OSError as exc:
            failure.append(exc)
        finally:
            if self.video_done.wait(10) and not failure:
                target = int((self.frames_written / self.settings.fps + AUDIO_TAIL_SECONDS) * SAMPLE_RATE)
                remaining = target - written
                while remaining > 0:
                    chunk = min(remaining, SAMPLE_RATE)
                    try:
                        q.put(bytes(chunk * BLOCK_ALIGN), timeout=2)
                    except queue.Full:
                        break
                    remaining -= chunk
            try:
                q.put(None, timeout=2)
            except queue.Full:
                pass
            wt.join(10)

    @property
    def video_duration(self):
        fps = self.settings.fps
        return max(0.0, (self.frames_written - round(self.trim_start * fps)) / fps)
