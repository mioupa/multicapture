import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime

from . import ffmpeg, win32
from .audiosession import SpeakerMute
from .browser import BrowserWindow, clone_profile
from .cdp import Browser, CDPError
from .config import Slot, data_dir, log_dir, login_profile_dir, work_dir
from .player import Player
from .recorder import SlotRecorder

WARMUP_SECONDS = 2.5
STALL_SECONDS = 0.2
POLL_SECONDS = 0.05
BOUNDARY_POLL_SECONDS = 0.002
PREROLL_SECONDS = 1.5
BYTES_PER_SECOND_1080P = 1_000_000
PLAY_OK = ("ok", "pending", None, True)
PREBUFFER_SECONDS = 5.0
RESUME_CONFIRM_SECONDS = 0.5
SEEK_SETTLE_SECONDS = 0.3
MIN_SEGMENT_SECONDS = 30
MAX_ATTEMPTS = 2
CASCADE = 36


def speaker_state_path():
    return os.path.join(data_dir(), "speaker_mute.json")


def restore_speakers_if_needed():
    if os.path.exists(speaker_state_path()):
        try:
            SpeakerMute(speaker_state_path()).restore()
        except OSError:
            pass


def cleanup_stale_work():
    root = work_dir()
    for name in os.listdir(root):
        shutil.rmtree(os.path.join(root, name), ignore_errors=True)


def fmt_time(seconds):
    s = int(max(0, seconds))
    return f"{s // 3600:d}:{s // 60 % 60:02d}:{s % 60:02d}"


def safe_name(text, fallback="lecture"):
    cleaned = "".join(c if c.isalnum() or c in "-_ ()[]（）「」" else "_" for c in (text or "")).strip(" _")
    return cleaned[:80] or fallback


class LoginBrowser:
    def __init__(self, exe_path, url):
        self.window = BrowserWindow(exe_path, login_profile_dir(), url, 80, 60, 1280, 900, app=False, debug=True)

    def open(self):
        self.window.launch()

    def is_open(self):
        return self.window.alive()

    def export_cookies(self):
        browser = Browser(self.window.devtools_url(timeout=5))
        try:
            return browser.get_cookies()
        finally:
            browser.close()

    def close(self):
        self.window.close(timeout=10)


@dataclass
class Segment:
    index: int
    start: float
    end: float
    status: str = "待機中"
    progress: float = 0.0
    path: str = None
    duration: float = 0.0
    error: str = None


class SplitJob:
    def __init__(self, url, count, width, height, pin_video, output_dir, settings, exe_path, ffmpeg_path, encoder,
                 silence, cookies, on_event):
        self.url = url
        self.count = max(1, count)
        self.width = width - width % 2
        self.height = height - height % 2
        self.pin_video = pin_video
        self.output_dir = output_dir
        self.settings = settings
        self.exe_path = exe_path
        self.ffmpeg_path = ffmpeg_path
        self.encoder = encoder
        self.silence = silence
        self.cookies = cookies or []
        self.on_event = on_event
        self.cancel_event = threading.Event()
        self.segments = []
        self.duration = 0.0
        self.output_path = None
        self.error = None
        self._thread = None
        self._windows = []
        self._recorders = []
        self._lock = threading.Lock()
        self._job_dir = os.path.join(work_dir(), uuid.uuid4().hex[:8])
        self._muter = SpeakerMute(speaker_state_path())

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        self._thread = threading.Thread(target=self._run, name="splitjob", daemon=True)
        self._thread.start()

    def cancel(self):
        self.cancel_event.set()
        with self._lock:
            for r in self._recorders:
                r.stop()

    def join(self, timeout=None):
        if self._thread:
            self._thread.join(timeout)

    def _emit(self, kind, payload=None):
        self.on_event(kind, payload)

    def _publish(self):
        self._emit("segments", [
            {"index": s.index, "start": s.start, "end": s.end, "status": s.status, "progress": s.progress}
            for s in self.segments
        ])

    def _status(self, text):
        self._emit("status", text)

    def _prepare_browser(self, i, results):
        try:
            profile = os.path.join(self._job_dir, f"seg{i + 1}")
            clone_profile(login_profile_dir(), profile)
            window = BrowserWindow(self.exe_path, profile, "about:blank", 40 + CASCADE * i, 40 + CASCADE * i,
                                   self.width, self.height, app=True, debug=True)
            window.launch()
            browser = Browser(window.devtools_url())
            if self.cookies:
                try:
                    browser.set_cookies(self.cookies)
                except CDPError:
                    pass
            player = Player(browser)
            player.navigate(self.url)
            info = player.find(timeout=90, cancelled=self.cancel_event.is_set)
            title = ""
            try:
                title = browser.evaluate(player.page_session(), "document.title", timeout=5) or ""
            except CDPError:
                pass
            results[i] = (window, browser, player, info, title)
        except Exception as exc:
            results[i] = exc

    def _run(self):
        prepared = []
        try:
            os.makedirs(self._job_dir, exist_ok=True)
            self._status(f"ブラウザを{self.count}個起動しています…")
            results = [None] * self.count
            threads = [threading.Thread(target=self._prepare_browser, args=(i, results), daemon=True) for i in range(self.count)]
            for t in threads:
                t.start()
                time.sleep(0.4)
            for t in threads:
                t.join()
            prepared = [r for r in results if isinstance(r, tuple)]
            for r in prepared:
                self._windows.append(r[0])
            if self.cancel_event.is_set():
                raise RuntimeError("中止しました")
            if not prepared:
                errors = [str(r) for r in results if isinstance(r, Exception)]
                raise RuntimeError(errors[0] if errors else "動画を開けませんでした")

            self.duration = max(r[3]["duration"] for r in prepared)
            title = next((r[4] for r in prepared if r[4]), "")
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            os.makedirs(self.output_dir, exist_ok=True)
            self.output_path = os.path.join(self.output_dir, f"{safe_name(title)}_{stamp}.mp4")

            needed = self.duration * BYTES_PER_SECOND_1080P * (self.width * self.height) / (1920 * 1080) * 2
            free = min(shutil.disk_usage(self.output_dir).free, shutil.disk_usage(self._job_dir).free)
            if free < needed:
                raise RuntimeError(
                    f"ディスクの空き容量が足りない可能性があります（空き {free / 2**30:.1f}GB、必要量の目安 {needed / 2**30:.1f}GB）。"
                    "保存先を変えるか、録画サイズを小さくしてください。"
                )

            n = max(1, min(len(prepared), int(self.duration // MIN_SEGMENT_SECONDS) or 1))
            length = self.duration / n
            self.segments = [Segment(i, i * length, self.duration if i == n - 1 else (i + 1) * length) for i in range(n)]
            for extra in prepared[n:]:
                extra[1].close()
                extra[0].close(timeout=5)
            prepared = prepared[:n]
            self._publish()
            self._emit("started", {"duration": self.duration, "segments": n, "title": title})
            self._status(f"動画の長さ {fmt_time(self.duration)} を {n} 分割して録画しています")

            if self.silence:
                try:
                    self._muter.mute()
                except OSError:
                    pass
            workers = [threading.Thread(target=self._segment_worker, args=(seg, prepared[seg.index]), daemon=True)
                       for seg in self.segments]
            for w in workers:
                w.start()
            last_publish = 0
            while any(w.is_alive() for w in workers):
                if time.monotonic() - last_publish > 0.5:
                    last_publish = time.monotonic()
                    self._publish()
                time.sleep(0.1)
            self._publish()

            if self.cancel_event.is_set():
                raise RuntimeError("中止しました")
            failed = [s for s in self.segments if not s.path]
            if failed:
                raise RuntimeError(f"区間 {', '.join(str(s.index + 1) for s in failed)} の録画に失敗しました: {failed[0].error}")

            self._status("区間をつなげて1本の動画にしています…")
            ffmpeg.concat(self.ffmpeg_path, [(s.path, s.duration) for s in self.segments], self.output_path, os.path.join(log_dir(), "split_concat.log"))
            self._emit("done", self.output_path)
        except Exception as exc:
            self.error = str(exc)
            self._emit("error", self.error)
        finally:
            try:
                self._muter.restore()
            except OSError:
                pass
            for item in prepared:
                try:
                    item[1].close()
                except Exception:
                    pass
            for window in self._windows:
                try:
                    window.close(timeout=5)
                except Exception:
                    pass
            time.sleep(1.0)
            shutil.rmtree(self._job_dir, ignore_errors=True)

    def _segment_worker(self, seg, prepared):
        window, browser, player, info, title = prepared
        win32.ensure_mta()
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if self.cancel_event.is_set():
                seg.status = "中止"
                return
            try:
                seg.path = self._record_segment(seg, window, player, attempt)
                seg.status = "完了"
                seg.progress = 1.0
                return
            except Exception as exc:
                seg.error = str(exc)
                seg.status = f"再試行中 ({exc})" if attempt < MAX_ATTEMPTS else f"失敗: {exc}"
        seg.path = None

    def _wait_for_media_time(self, player, recorder, target, timeout=180.0):
        deadline = time.monotonic() + timeout
        last_kick = time.monotonic()
        while time.monotonic() < deadline:
            if self.cancel_event.is_set():
                raise RuntimeError("中止しました")
            if not recorder.running:
                raise RuntimeError(recorder.error or "録画が停止しました")
            st = player.state()
            if not st.get("ok"):
                raise RuntimeError("動画プレーヤーを見失いました")
            t = float(st.get("t", 0.0))
            if (t >= target and (target > 0 or t > 0)) or st.get("ended"):
                return t
            if st.get("paused") and time.monotonic() - last_kick > 2.0:
                last_kick = time.monotonic()
                player.play()
            remaining = target - t
            time.sleep(BOUNDARY_POLL_SECONDS if remaining < 0.15 else min(POLL_SECONDS, remaining - 0.12))
        raise RuntimeError("再生が始まりませんでした")

    def _record_segment(self, seg, window, player, attempt):
        seg.status = "頭出し中"
        seg.progress = 0.0
        preroll_from = max(0.0, seg.start - PREROLL_SECONDS)
        if not player.seek(preroll_from):
            raise RuntimeError("再生位置を移動できませんでした")
        if self.pin_video:
            try:
                player.pin_video()
            except CDPError:
                pass
            time.sleep(0.5)
            player.seek(preroll_from)
        player.wait_buffered(PREBUFFER_SECONDS, seg.end, 10, self.cancel_event.is_set)

        part = os.path.join(self._job_dir, f"part{seg.index + 1:02d}_{attempt}.mp4")
        recorder = SlotRecorder(
            seg.index, Slot(f"part{seg.index + 1}", self.url, self.width, self.height), window, self.settings,
            self.ffmpeg_path, self.encoder, lambda i, text: None,
            output_path=part, trim_start=WARMUP_SECONDS, log_name=f"split_part{seg.index + 1}.log",
        )
        with self._lock:
            self._recorders.append(recorder)
        recorder.start()
        try:
            if not recorder.ready.wait(30):
                raise RuntimeError(recorder.error or "録画を開始できませんでした")
            recorder.pause_at_frame(round(WARMUP_SECONDS * self.settings.fps))
            deadline = time.monotonic() + 30
            while not recorder.paused:
                if not recorder.running or time.monotonic() > deadline:
                    raise RuntimeError(recorder.error or "録画を開始できませんでした")
                time.sleep(0.005)
            result = player.play()
            if result not in PLAY_OK:
                raise RuntimeError(f"再生できませんでした: {result}")
            t = self._wait_for_media_time(player, recorder, seg.start)
            recorder.resume()
            seg.status = "録画中"
            last_t = t
            last_change = time.monotonic()
            stalled_at = None
            last_kick = 0.0
            self.stalls = getattr(self, "stalls", 0)

            while True:
                if self.cancel_event.is_set():
                    raise RuntimeError("中止しました")
                if not recorder.running:
                    raise RuntimeError(recorder.error or "録画が停止しました")
                st = player.state()
                if not st.get("ok"):
                    raise RuntimeError("動画プレーヤーを見失いました")
                now = time.monotonic()
                t = float(st.get("t", 0.0))
                if stalled_at is None:
                    if t >= seg.end or st.get("ended"):
                        recorder.pause()
                        player.pause()
                        break
                    if t > last_t + 0.002:
                        last_t = t
                        last_change = now
                    elif now - last_change > STALL_SECONDS or (st.get("paused") and not st.get("ended")):
                        recorder.pause()
                        stalled_at = last_t
                        self.stalls += 1
                        seg.status = "読み込み待ち"
                else:
                    if st.get("paused") and not st.get("ended") and now - last_kick > 2.0:
                        last_kick = now
                        player.play()
                    if t > stalled_at + RESUME_CONFIRM_SECONDS or st.get("ended") or t >= seg.end - 0.005:
                        player.pause()
                        player.seek(stalled_at)
                        time.sleep(SEEK_SETTLE_SECONDS)
                        recorder.resume()
                        result = player.play()
                        if result not in PLAY_OK:
                            raise RuntimeError(f"再生できませんでした: {result}")
                        last_t = stalled_at
                        last_change = time.monotonic()
                        stalled_at = None
                        seg.status = "録画中"
                        continue
                if t < seg.start - 2.0:
                    recorder.pause()
                    stalled_at = last_t
                seg.progress = min(1.0, max(0.0, (t - seg.start) / max(0.001, seg.end - seg.start)))
                remaining = seg.end - t
                time.sleep(BOUNDARY_POLL_SECONDS if remaining < 0.15 else min(POLL_SECONDS, max(BOUNDARY_POLL_SECONDS, remaining - 0.12)))
        finally:
            recorder.stop()
            recorder.join(120)

            with self._lock:
                if recorder in self._recorders:
                    self._recorders.remove(recorder)
        if recorder.error:
            raise RuntimeError(recorder.error)
        seg.status = "保存完了"
        seg.duration = recorder.video_duration
        return part
