"""Reader for the shared-memory frame ring written by mc-spike (--shm). See README."""
import mmap
import os
import struct
import time

MAGIC = b"MCRING01"
HDR = struct.Struct("<8sIIIIIII")   # magic, version, slot_count, slot_size, header_size, pix_fmt, w, h  (0..32)
SLOT_HDR = struct.Struct("<QQdIIIIIII")  # seqlock, frame_seq, pts, w, h, stride0, stride1, plane0_off, plane1_off, data_bytes


class RingError(RuntimeError):
    pass


class RingReader:
    def __init__(self, path, wait=0.0):
        deadline = time.monotonic() + wait
        while not os.path.exists(path) or os.path.getsize(path) < 64:
            if time.monotonic() > deadline:
                raise RingError(f"ring file not ready: {path}")
            time.sleep(0.05)
        self.path = path
        self._f = open(path, "rb")
        self.mm = mmap.mmap(self._f.fileno(), 0, access=mmap.ACCESS_READ)
        magic, ver, self.slot_count, self.slot_size, self.header_size, self.pix_fmt, self.width, self.height = \
            HDR.unpack_from(self.mm, 0)
        if magic != MAGIC or ver != 1 or self.slot_count == 0 or self.header_size < 64:
            raise RingError(f"bad ring header: magic={magic!r} version={ver} slots={self.slot_count}")
        if len(self.mm) < self.header_size + self.slot_count * self.slot_size:
            raise RingError("ring file shorter than header claims")
        self.torn = 0          # torn reads detected (each retry counts)
        self.torn_failed = 0   # frames given up on after the retries
        self.lost = 0          # frames overwritten before being read
        self.frames_read = 0

    def close(self):
        try:
            self.mm.close()
            self._f.close()
        except Exception:
            pass

    def latest_seq(self):
        return struct.unpack_from("<Q", self.mm, 40)[0]

    def _slot_off(self, i):
        return self.header_size + i * self.slot_size

    def read_slot(self, i, retries=3, copy_bytes=None):
        """Seqlock read of slot i. -> dict(seq, pts, w, h, stride0, stride1, plane0_off, plane1_off,
        data) or None if empty / torn after retries. plane offsets are relative to the pixel data start.
        (If the writer stores offsets relative to the slot start, i.e. >= 64 for plane0, we normalise.)"""
        off = self._slot_off(i)
        for attempt in range(retries + 1):
            s1 = struct.unpack_from("<Q", self.mm, off)[0]
            if s1 & 1:
                self.torn += 1
                continue
            sh = SLOT_HDR.unpack_from(self.mm, off)
            _, fseq, pts, w, h, st0, st1, p0, p1, nbytes = sh
            if fseq == 0:
                return None
            if p0 >= 64:  # offsets stored relative to slot start: normalise to pixel-data start
                p0 -= 64
                p1 = p1 - 64 if p1 >= 64 else p1
            n = nbytes if copy_bytes is None else min(nbytes, copy_bytes)
            if n > self.slot_size:
                raise RingError("slot header inconsistent")
            data = self.mm[off + 64: off + 64 + n]
            s2 = struct.unpack_from("<Q", self.mm, off)[0]
            if s1 != s2:
                self.torn += 1
                continue
            return {"seq": fseq, "pts": pts, "w": w, "h": h, "stride0": st0, "stride1": st1,
                    "plane0_off": p0, "plane1_off": p1, "data": data, "seqlock": s1}
        self.torn_failed += 1
        return None

    def iter_new(self, last_seq, copy_bytes=None):
        """Read every frame with frame_seq > last_seq still in the ring.
        -> (frames oldest first, new_last_seq). Frames overwritten before being read are added
        to self.lost."""
        latest = self.latest_seq()
        if latest <= last_seq:
            return [], last_seq
        first = max(last_seq + 1, latest - self.slot_count + 1)
        if last_seq > 0 or first > 1:
            self.lost += max(0, first - (last_seq + 1))
        frames = []
        for seq in range(first, latest + 1):
            fr = self.read_slot(seq % self.slot_count, copy_bytes=copy_bytes)
            if fr is None or fr["seq"] != seq:
                if fr is not None and fr["seq"] > seq:
                    self.lost += 1       # overwritten by a newer frame meanwhile
                elif fr is None:
                    self.lost += 1       # torn beyond retries (also counted in torn_failed)
                continue
            frames.append(fr)
            self.frames_read += 1
        return frames, latest


class PyRingWriter:
    """Python writer following the README procedure (used for tests when the helper is unavailable)."""

    def __init__(self, path, width, height, slot_count=6):
        self.slot_count, self.w, self.h = slot_count, width, height
        self.data_bytes = width * height * 3 // 2
        self.slot_size = 64 + self.data_bytes
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600)
        os.ftruncate(fd, 64 + slot_count * self.slot_size)
        self.mm = mmap.mmap(fd, 0)
        os.close(fd)
        HDR.pack_into(self.mm, 0, MAGIC, 1, slot_count, self.slot_size, 64, 0, width, height)
        self.seq = 0

    def write(self, pts=0.0):
        self.seq += 1
        off = 64 + (self.seq % self.slot_count) * self.slot_size
        sl = struct.unpack_from("<Q", self.mm, off)[0]
        struct.pack_into("<Q", self.mm, off, sl + 1)
        SLOT_HDR.pack_into(self.mm, off, sl + 1, self.seq, pts, self.w, self.h, self.w, self.w, 0, self.w * self.h,
                           self.data_bytes)
        y = bytes([self.seq % 256]) * (self.w * self.h)
        self.mm[off + 64: off + 64 + self.w * self.h] = y
        self.mm[off + 64 + self.w * self.h: off + 64 + self.data_bytes] = b"\x80" * (self.w * self.h // 2)
        struct.pack_into("<Q", self.mm, off, sl + 2)
        struct.pack_into("<Q", self.mm, 40, self.seq)
