import os
import queue
import subprocess
import threading
import time
from datetime import datetime

from . import ffmpeg
from . import platform as osp
from .config import log_dir

SAMPLE_RATE = 48000
CHANNELS = 2
BLOCK_ALIGN = CHANNELS * 2
AUDIO_QUEUE_CHUNKS = 3000
AUDIO_TAIL_SECONDS = 0.3
AUDIO_DELAY_SECONDS = -0.025
AUDIO_MAX_GAP_SECONDS = 30.0
POLL_INTERVAL = 0.004
VIDEO_LATENCY_SECONDS = 0.08
MAX_QUEUED_FRAMES = 4
VIDEO_OFFSET_SMOOTHING = 0.1
SEQ_CORRECT_THRESHOLD = 0.6
VIDEO_OFFSET_FAST = 0.3
VIDEO_OFFSET_SETTLE_FRAMES = 20
MAX_CATCH_UP_SECONDS = 15


def even(n):
    return max(2, n - (n % 2))


class AudioPlacer:
    """Places captured audio chunks on the recording timeline (gaps -> silence, overlaps -> trimmed)."""

    def __init__(self, position_of, pause_started_after, t0, sequential, emit):
        self.position_of = position_of
        self.pause_started_after = pause_started_after
        self.t0 = t0
        self.sequential = sequential
        self.emit = emit
        self.written = 0

    def place(self, data, stamp):
        frames = len(data) // BLOCK_ALIGN
        shift = AUDIO_DELAY_SECONDS if self.sequential else 0.0
        while frames > 0:
            pos, resume_at = self.position_of(stamp + shift)
            if pos is None:
                if resume_at is None:
                    if self.t0 is not None and stamp + shift < self.t0:
                        skip = min(frames, int((self.t0 - stamp - shift) * SAMPLE_RATE) + 1)
                    else:
                        return
                else:
                    skip = min(frames, max(1, int(round((resume_at - stamp - shift) * SAMPLE_RATE))))
                data = data[skip * BLOCK_ALIGN:]
                frames -= skip
                stamp += skip / SAMPLE_RATE
                continue
            cut = self.pause_started_after(stamp + shift)
            keep = frames
            if cut is not None:
                keep = max(0, min(frames, int(round((cut - stamp - shift) * SAMPLE_RATE))))
            target = int(round(pos * SAMPLE_RATE))
            head = data[:keep * BLOCK_ALIGN]
            if target > self.written:
                gap = target - self.written
                if gap <= AUDIO_MAX_GAP_SECONDS * SAMPLE_RATE:
                    self.emit(bytes(gap * BLOCK_ALIGN))
                    self.written += gap
            elif target < self.written:
                overlap = min(keep, self.written - target)
                head = head[overlap * BLOCK_ALIGN:]
            if head:
                self.emit(head)
                self.written += len(head) // BLOCK_ALIGN
            data = data[keep * BLOCK_ALIGN:]
            frames -= keep
            stamp += keep / SAMPLE_RATE
            if keep == 0:
                pos2, _ = self.position_of(stamp + shift)
                if pos2 is not None:
                    return


class SlotRecorder:
    def __init__(self, index, slot, browser_window, settings, ffmpeg_path, encoder, on_status,
                 output_path=None, trim_start=0.0, log_name=None, key_frames=None, sequential=False, mute_audio=False):
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
        self.key_frames = key_frames
        self.sequential = sequential
        self.mute_audio = mute_audio
        self.log_name = log_name or f"slot{index + 1}_ffmpeg.log"
        self.error = None
        self.frames_written = 0
        self.new_frames = 0
        self.dropped_frames = 0
        self.ring_overflow = 0
        self.audio_dropped = 0
        self.ready = threading.Event()
        self.video_done = threading.Event()
        self._thread = None
        self._clock_lock = threading.Lock()
        self._t0 = None
        self._pauses = []
        self._pause_at = None
        self._video_offset = None

    def _timeline_at(self, wall):
        total = wall - self._t0
        for start, end in self._pauses:
            if start >= wall:
                continue
            total -= min(wall if end is None else end, wall) - start
        return total

    def elapsed(self):
        with self._clock_lock:
            if self._t0 is None:
                return 0.0
            return self._timeline_at(osp.clock.now())

    def position_of(self, wall):
        with self._clock_lock:
            if self._t0 is None or wall < self._t0:
                return None, None
            for start, end in self._pauses:
                if start <= wall and (end is None or wall < end):
                    return None, end
            return self._timeline_at(wall), None

    def pause_started_after(self, wall):
        with self._clock_lock:
            for start, end in self._pauses:
                if start > wall:
                    return start
            return None

    @property
    def paused(self):
        return bool(self._pauses) and self._pauses[-1][1] is None

    def pause(self):
        with self._clock_lock:
            if not (self._pauses and self._pauses[-1][1] is None):
                self._pauses.append([osp.clock.now(), None])

    def _pause_snapped(self, timeline):
        with self._clock_lock:
            if self._t0 is None or (self._pauses and self._pauses[-1][1] is None):
                return
            now = osp.clock.now()
            current = self._timeline_at(now)
            self._pauses.append([now - max(0.0, current - timeline), None])

    def pause_at_frame(self, frame_index):
        self._pause_at = frame_index

    def resume(self):
        with self._clock_lock:
            if self._pauses and self._pauses[-1][1] is None:
                self._pauses[-1][1] = osp.clock.now()

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
        capture = loop = pipe = proc = None
        log = None
        audio_thread = None
        try:
            osp.thread_init()
            fps = self.settings.fps
            out_w, out_h = even(self.slot.width), even(self.slot.height)
            cw, ch = self.browser.fit_content(out_w, out_h)
            cap_w, cap_h = even(min(cw, out_w)), even(min(ch, out_h))

            capture = osp.open_window_capture(self.browser)
            capture.fps = fps  # used by the macOS helper (capture rate); ignored on Windows
            capture.start()
            capture.set_output(cap_w, cap_h)
            offset = self.browser.capture_offset() or (0, 0)
            capture.set_region(*offset)

            loop = osp.open_audio_capture(self.browser, SAMPLE_RATE, CHANNELS, mute=self.mute_audio)
            loop.start()

            if not self.output_path:
                os.makedirs(self.settings.output_dir, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in self.slot.name) or f"slot{self.index + 1}"
                self.output_path = os.path.join(self.settings.output_dir, f"{safe}_{stamp}.mp4")

            pipe = osp.AudioPipe()
            cmd = ffmpeg.build_command(
                self.ffmpeg_path, cap_w, cap_h, fps, pipe.path, SAMPLE_RATE, CHANNELS,
                out_w, out_h, self.encoder, self.output_path, trim_start=self.trim_start,
                no_bframes=self.trim_start > 0, key_frames=self.key_frames, pix_fmt=capture.pix_fmt,
            )
            log = open(os.path.join(log_dir(), self.log_name), "w", encoding="utf-8", errors="replace")
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log,
                **osp.popen_kwargs(),
            )

            with self._clock_lock:
                self._t0 = osp.clock.now()
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
                log.write(
                    f"[multicapture] frames_written={self.frames_written} dropped_frames={self.dropped_frames} "
                    f"ring_overflow={self.ring_overflow} audio_dropped={self.audio_dropped}\n"
                )
                log.close()
            for obj in (loop, capture):
                if obj:
                    try:
                        obj.close()
                    except Exception:
                        pass
            if not self.error:
                self.status("保存完了" if self.output_path else "停止")

    def _video_loop(self, capture, proc, fps, label):
        write = proc.stdin.write
        last_geometry = osp.clock.now()
        last_report = last_geometry
        period = 1.0 / fps
        next_seq = None
        last_slot = None
        settle = 0
        self._video_offset = None
        while not self.stop_event.is_set():
            now = osp.clock.now()
            if now - last_geometry > 1.0:
                last_geometry = now
                if not self.browser.alive():
                    raise RuntimeError("ブラウザが閉じられました")
                self.browser.restore_if_minimized()
                offset = self.browser.capture_offset()
                if offset:
                    capture.set_region(*offset)
            if capture.update():
                self.new_frames += 1
            if self.paused:
                next_seq = None
                time.sleep(POLL_INTERVAL)
                continue
            due = int((self.elapsed() - (VIDEO_LATENCY_SECONDS if self.sequential else 0.0)) * fps) + 1
            behind = due - self.frames_written
            if behind > fps * MAX_CATCH_UP_SECONDS:
                self.dropped_frames += due - fps - self.frames_written
                self.frames_written = due - fps
                behind = fps
            if behind > 0:
                base = osp.clock.now() - self.elapsed()
            for _ in range(max(0, behind)):
                if self.stop_event.is_set():
                    break
                if self.paused:
                    break
                if self._pause_at is not None and self.frames_written >= self._pause_at:
                    self._pause_at = None
                    self._pause_snapped(self.frames_written / fps)
                    break
                nominal = base + self.frames_written / fps
                if not self.sequential:
                    capture.write_slot(write, capture.slot_at(nominal))
                    self.frames_written += 1
                    continue
                if next_seq is None:
                    self._video_offset = None
                    first = capture.first_seq_after(nominal - period / 2)
                    next_seq = first if first is not None else capture.seq + 1
                oldest = capture.oldest_seq()
                if oldest is not None and next_seq < oldest:
                    self.ring_overflow += oldest - next_seq
                    next_seq = oldest
                if capture.seq - next_seq >= MAX_QUEUED_FRAMES:
                    self.dropped_frames += capture.seq - 1 - next_seq
                    next_seq = capture.seq - 1
                if self._video_offset is not None and last_slot is not None:
                    if self._video_offset > SEQ_CORRECT_THRESHOLD * period:
                        self._video_offset -= period
                        capture.write_slot(write, last_slot)
                        self.frames_written += 1
                        continue
                    if self._video_offset < -SEQ_CORRECT_THRESHOLD * period:
                        self._video_offset += period
                        next_seq += 1
                slot = capture.slot_of_seq(next_seq)
                if slot is not None:
                    next_seq += 1
                    last_slot = slot
                    diff = capture.ring_times[slot] - nominal
                    if abs(diff) < 0.5:
                        if self._video_offset is None:
                            self._video_offset = diff
                            settle = 0
                        else:
                            settle += 1
                            alpha = VIDEO_OFFSET_FAST if settle < VIDEO_OFFSET_SETTLE_FRAMES else VIDEO_OFFSET_SMOOTHING
                            self._video_offset += alpha * (diff - self._video_offset)
                elif last_slot is None or capture.ring_seq[last_slot] < 0:
                    slot = capture.head
                    last_slot = slot
                else:
                    slot = last_slot
                capture.write_slot(write, slot)
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
        osp.thread_init()

        def emit(data):
            try:
                q.put_nowait(data)
            except queue.Full:
                self.audio_dropped += 1

        placer = AudioPlacer(self.position_of, self.pause_started_after, self._t0, self.sequential, emit)

        try:
            while not self.stop_event.is_set():
                for data, stamp in loop.read_timed():
                    placer.place(data, stamp)
                time.sleep(0.005)
            for data, stamp in loop.read_timed():
                placer.place(data, stamp)
        except OSError as exc:
            failure.append(exc)
        finally:
            if self.video_done.wait(10) and not failure:
                target = int((self.frames_written / self.settings.fps + (AUDIO_TAIL_SECONDS if self.sequential else 0.0)) * SAMPLE_RATE)
                remaining = target - placer.written
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
