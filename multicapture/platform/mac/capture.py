"""WindowCapture / AudioCapture adapters over the mc-capture helper (shared-memory ring and FIFO)."""
import math
import mmap
import os
import select
import shutil
import struct
import tempfile
import threading
import time

from . import helper

RING_SIZE = 6
RING_MAGIC = b"MCRING01"
FILE_HDR = struct.Struct("<8sIIIIIII")                 # magic, version, slots, slot_size, header_size, pix_fmt, w, h
SLOT_HDR = struct.Struct("<QQdIIIIIII")                # seqlock, frame_seq, pts, w, h, stride0, stride1, off0, off1, bytes
LATEST_SEQ_OFFSET = 40
SLOT_HEADER_SIZE = 64

AUDIO_MAGIC = 0x5541434D
AUDIO_MAGIC_BYTES = struct.pack("<I", AUDIO_MAGIC)
AUDIO_HDR = struct.Struct("<IdI")                      # magic, timestamp, frames
AUDIO_BLOCK = 4
AUDIO_MAX_FRAMES = 1 << 20


def _clock():
    from . import clock
    return clock


# ============================================================================ video
class RingReader:
    """Read-only view of the helper's frame ring (seqlock per slot)."""

    def __init__(self, path):
        self.path = path
        self._f = open(path, "rb")
        try:
            self.mm = mmap.mmap(self._f.fileno(), 0, access=mmap.ACCESS_READ)
        except (ValueError, OSError):
            self._f.close()
            raise
        magic, version, self.slot_count, self.slot_size, self.header_size, self.pix_fmt, self.width, self.height = \
            FILE_HDR.unpack_from(self.mm, 0)
        if magic != RING_MAGIC or version != 1 or self.slot_count == 0 or self.header_size < 64:
            self.close()
            raise RuntimeError("共有メモリのリングの形式が正しくありません")
        if len(self.mm) < self.header_size + self.slot_count * self.slot_size:
            self.close()
            raise RuntimeError("共有メモリのリングが短すぎます")

    def close(self):
        try:
            self.mm.close()
        except (ValueError, OSError):
            pass
        try:
            self._f.close()
        except OSError:
            pass

    def latest_seq(self):
        return struct.unpack_from("<Q", self.mm, LATEST_SEQ_OFFSET)[0]

    def slot_offset(self, index):
        return self.header_size + index * self.slot_size

    def read_header(self, index, retries=3):
        """Seqlock-validated slot header as a tuple, or None when it is being written / empty."""
        off = self.slot_offset(index)
        for _ in range(retries):
            s1 = struct.unpack_from("<Q", self.mm, off)[0]
            if s1 & 1:
                continue
            hdr = SLOT_HDR.unpack_from(self.mm, off)
            if struct.unpack_from("<Q", self.mm, off)[0] != s1:
                continue
            return hdr if hdr[1] != 0 else None
        return None

    def copy_slot(self, index):
        """(bytes of the packed NV12 frame, header tuple, consistent). One retry on a torn read, then the
        copy is returned anyway with consistent=False."""
        off = self.slot_offset(index)
        data, hdr, ok = b"", None, False
        for attempt in range(2):
            s1 = struct.unpack_from("<Q", self.mm, off)[0]
            hdr = SLOT_HDR.unpack_from(self.mm, off)
            data = self._pixels(off, hdr)
            s2 = struct.unpack_from("<Q", self.mm, off)[0]
            if s1 == s2 and not (s1 & 1):
                ok = True
                break
        return data, hdr, ok

    def _pixels(self, off, hdr):
        _, _, _, w, h, stride0, stride1, off0, off1, nbytes = hdr
        if w == 0 or h == 0:
            return b""
        # plane offsets are from the slot start (PROTOCOL.md); tolerate data-relative ones
        if off0 >= SLOT_HEADER_SIZE:
            p0, p1 = off0, off1
        else:
            p0, p1 = SLOT_HEADER_SIZE + off0, SLOT_HEADER_SIZE + off1
        total = w * h * 3 // 2
        if stride0 == w and stride1 == w and p1 == p0 + w * h and nbytes >= total:
            if p0 + total > self.slot_size:
                return b""
            return self.mm[off + p0: off + p0 + total]
        rows = [self.mm[off + p0 + r * stride0: off + p0 + r * stride0 + w] for r in range(h)]
        rows += [self.mm[off + p1 + r * stride1: off + p1 + r * stride1 + w] for r in range(h // 2)]
        return b"".join(rows)


class MacWindowCapture:
    pix_fmt = "nv12"

    def __init__(self, browser_window, log_name=None):
        self.bw = browser_window
        self._log_name = log_name or f"mc-capture_{browser_window.pid}.log"
        self.fps = 30                 # recorder sets this before start(); see start_video
        self.session = None
        self.ring = None
        self._tmpdir = None
        self.shm_path = None
        self.out_width = 0
        self.out_height = 0
        self.region = (0, 0)
        self._sent_region = None
        self.video_started = False
        self.ring_times = [float("-inf")] * RING_SIZE
        self.ring_seq = [-1] * RING_SIZE
        self._helper_seq = [0] * RING_SIZE
        self.seq = 0
        self.head = 0
        self._last_hseq = 0
        self.lost = 0           # frames overwritten before update() saw them
        self.torn = 0           # torn reads that had to be retried
        self.torn_written = 0   # frames written although the seqlock never validated
        self.overwritten = 0    # write_slot found a newer frame in the slot

    # ---------------------------------------------------------------- lifecycle
    def start(self):
        if self.session is None:
            self.session = helper.acquire_session(self.bw, self._log_name)

    def set_output(self, width, height):
        self.out_width, self.out_height = int(width), int(height)

    def set_region(self, left, top):
        self.region = (max(0, int(left)), max(0, int(top)))
        if self.session is None or not self.out_width:
            return
        crop = [self.region[0], self.region[1], self.out_width, self.out_height]
        if not self.video_started:
            self._start_video(crop)
        elif self._sent_region != self.region:
            self.session.update_video(crop)
            self._sent_region = self.region

    def _start_video(self, crop):
        window_id = self._window_id()
        self._tmpdir = tempfile.mkdtemp(prefix="multicapture-")
        self.shm_path = os.path.join(self._tmpdir, "video.ring")
        self.session.start_video(window_id, self.out_width, self.out_height, crop, self.fps, self.shm_path)
        self.video_started = True
        self._sent_region = self.region
        deadline = time.monotonic() + 10
        while self.ring is None:
            try:
                if os.path.getsize(self.shm_path) >= 64:
                    self.ring = RingReader(self.shm_path)
                    break
            except (OSError, RuntimeError):
                pass
            if time.monotonic() > deadline or not self.session.alive:
                raise RuntimeError("映像の共有メモリが作られませんでした")
            time.sleep(0.05)

    def _window_id(self):
        finder = getattr(self.bw, "find_window_id", None)
        wid = finder() if finder else getattr(self.bw, "window_id", None)
        if not wid:
            raise RuntimeError("録画するウィンドウを特定できませんでした")
        return wid

    # ---------------------------------------------------------------- ring bookkeeping (mirrors wgc.py)
    def update(self):
        ring = self.ring
        if ring is None:
            return False
        latest = ring.latest_seq()
        if latest <= self._last_hseq:
            return False
        first = max(self._last_hseq + 1, latest - ring.slot_count + 1)
        if self._last_hseq > 0 and first > self._last_hseq + 1:
            self.lost += first - self._last_hseq - 1
        got = False
        for hseq in range(first, latest + 1):
            slot = hseq % ring.slot_count
            hdr = ring.read_header(slot)
            if hdr is None or hdr[1] != hseq:
                self.lost += 1
                continue
            pts = hdr[2]
            if not math.isfinite(pts) or pts <= 0:
                pts = _clock().now()
            self.seq += 1
            self.ring_times[slot] = pts
            self.ring_seq[slot] = self.seq
            self._helper_seq[slot] = hseq
            self.head = slot
            got = True
        self._last_hseq = latest
        return got

    def slot_of_seq(self, seq):
        for slot in range(RING_SIZE):
            if self.ring_seq[slot] == seq:
                return slot
        return None

    def oldest_seq(self):
        valid = [s for s in self.ring_seq if s >= 0]
        return min(valid) if valid else None

    def first_seq_after(self, wall):
        candidates = [(self.ring_seq[i], self.ring_times[i]) for i in range(RING_SIZE)
                      if self.ring_seq[i] >= 0 and self.ring_times[i] >= wall]
        return min(candidates)[0] if candidates else None

    def slot_at(self, wall):
        best = None
        for slot in range(RING_SIZE):
            if self.ring_seq[slot] >= 0 and self.ring_times[slot] <= wall:
                if best is None or self.ring_times[slot] > self.ring_times[best]:
                    best = slot
        if best is not None:
            return best
        valid = [s for s in range(RING_SIZE) if self.ring_seq[s] >= 0]
        return min(valid, key=lambda s: self.ring_times[s]) if valid else self.head

    def write_slot(self, sink, slot):
        ring = self.ring
        if ring is None:
            return
        data, hdr, ok = ring.copy_slot(slot)
        if not ok:
            self.torn += 1
            self.torn_written += 1
        if hdr is not None and hdr[1] != self._helper_seq[slot] and ok:
            self.overwritten += 1
        expected = self.out_width * self.out_height * 3 // 2
        if len(data) != expected:
            data = (data + bytes(expected))[:expected]
        sink(data)

    def close(self):
        session, self.session = self.session, None
        if session is not None:
            if self.video_started:
                try:
                    session.stop_video(timeout=3)
                except helper.HelperError:
                    pass
            helper.release_session(self.bw, session)
        ring, self.ring = self.ring, None
        if ring is not None:
            ring.close()
        if self._tmpdir:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None
        self.video_started = False


def open_window_capture(browser_window):
    return MacWindowCapture(browser_window)


# ============================================================================ audio
class FifoParser:
    """Parses the helper's audio records: u32 magic, f64 timestamp, u32 frames, frames * 4 bytes (s16le stereo)."""

    def __init__(self):
        self._buf = bytearray()
        self.resyncs = 0

    def feed(self, data):
        self._buf += data
        out = []
        buf = self._buf
        while len(buf) >= AUDIO_HDR.size:
            magic, stamp, frames = AUDIO_HDR.unpack_from(buf, 0)
            if magic != AUDIO_MAGIC or frames > AUDIO_MAX_FRAMES or not math.isfinite(stamp):
                self._resync(buf)
                continue
            end = AUDIO_HDR.size + frames * AUDIO_BLOCK
            if len(buf) < end:
                break
            out.append((bytes(buf[AUDIO_HDR.size:end]), stamp))
            del buf[:end]
        return out

    def _resync(self, buf):
        self.resyncs += 1
        index = buf.find(AUDIO_MAGIC_BYTES, 1)
        if index < 0:
            keep = len(AUDIO_MAGIC_BYTES) - 1
            del buf[:max(1, len(buf) - keep)]
        else:
            del buf[:index]


class MacAudioCapture:
    def __init__(self, browser_window, sample_rate, channels, mute=False, log_name=None):
        self.bw = browser_window
        self.sample_rate = sample_rate
        self.channels = channels
        self.mute = mute
        self._log_name = log_name or f"mc-capture_{browser_window.pid}.log"
        self.session = None
        self._dir = None
        self._path = None
        self._fd = None
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._chunks = []
        self.parser = FifoParser()
        self.started = False
        self.bytes_read = 0

    def start(self, timeout=5.0):
        if self.sample_rate != 48000 or self.channels != 2:
            raise RuntimeError("macOSの音声取り込みは48kHzステレオのみ対応しています")
        self.session = helper.acquire_session(self.bw, self._log_name)
        self._dir = tempfile.mkdtemp(prefix="multicapture-")
        self._path = os.path.join(self._dir, "audio.fifo")
        os.mkfifo(self._path, 0o600)
        # O_RDWR never blocks and keeps a writer alive, so the reader never sees EOF before the helper connects
        self._fd = os.open(self._path, os.O_RDWR | os.O_NONBLOCK)
        self._thread = threading.Thread(target=self._read_loop, name="mc-audio-fifo", daemon=True)
        self._thread.start()
        mode = "mutedWhenTapped" if self.mute else "unmuted"
        try:
            self.session.start_audio(self.bw.pid, mode, self._path, timeout=timeout)
            self.started = True
        except helper.HelperError as exc:
            if "応答がありません" not in str(exc):
                raise RuntimeError(f"音声の取り込みを開始できませんでした: {exc}") from exc
            # the helper keeps looking for the audio process for up to 15 s; do not fail the recording

    def _read_loop(self):
        fd = self._fd
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([fd], [], [], 0.05)
                if not ready:
                    continue
                data = os.read(fd, 1 << 16)
            except (BlockingIOError, InterruptedError):
                continue
            except (OSError, ValueError):
                return
            if not data:
                time.sleep(0.01)
                continue
            self.bytes_read += len(data)
            records = self.parser.feed(data)
            if records:
                with self._lock:
                    self._chunks.extend(records)

    def read_timed(self):
        with self._lock:
            chunks, self._chunks = self._chunks, []
        return chunks

    def close(self):
        session, self.session = self.session, None
        if session is not None:
            if self.started:
                try:
                    session.stop_audio(timeout=3)
                except helper.HelperError:
                    pass
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2)
        fd, self._fd = self._fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if self._dir:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None
        if session is not None:
            helper.release_session(self.bw, session)


def open_audio_capture(browser_window, sample_rate, channels, mute=False):
    return MacAudioCapture(browser_window, sample_rate, channels, mute)
