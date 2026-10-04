#!/usr/bin/env python3
"""Fake `mc-capture` for tests: speaks native/mac/PROTOCOL.md with synthetic data.

Video: NV12 frames in a real shared-memory ring. The Y plane is filled with (frame_seq % 256) and its first
four bytes hold frame_seq as u32 LE; UV is 128.  Audio: a 1 kHz tone as FIFO records.
Env FAKE_AUDIO_GARBAGE=1 injects junk bytes into the FIFO once.
"""
import json
import math
import mmap
import os
import struct
import sys
import threading
import time

RING = 6
HDR = struct.Struct("<8sIIIIIII")
SLOT = struct.Struct("<QQdIIIIIII")
CMD_KEYS = ("cmd", "command")


def now():
    return time.clock_gettime(time.CLOCK_UPTIME_RAW)


out_lock = threading.Lock()


def emit(ev, **kw):
    msg = {"ev": ev, "t": now(), **kw}
    with out_lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


class Video(threading.Thread):
    def __init__(self, shm, w, h, fps, ident):
        super().__init__(daemon=True)
        self.w, self.h, self.fps, self.shm = w, h, fps, shm
        self.stop = threading.Event()
        self.data_bytes = w * h * 3 // 2
        self.slot_size = 64 + self.data_bytes
        fd = os.open(shm, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600)
        os.ftruncate(fd, 64 + RING * self.slot_size)
        self.mm = mmap.mmap(fd, 0)
        os.close(fd)
        HDR.pack_into(self.mm, 0, b"MCRING01", 1, RING, self.slot_size, 64, 0, w, h)
        self.complete = 0

    def run(self):
        seq = 0
        period = 1.0 / self.fps
        nxt = time.monotonic()
        last_stats = time.monotonic()
        while not self.stop.is_set():
            seq += 1
            off = 64 + (seq % RING) * self.slot_size
            sl = struct.unpack_from("<Q", self.mm, off)[0]
            struct.pack_into("<Q", self.mm, off, sl + 1)
            SLOT.pack_into(self.mm, off, sl + 1, seq, now(), self.w, self.h, self.w, self.w, 64, 64 + self.w * self.h,
                           self.data_bytes)
            y = bytearray([seq % 256]) * (self.w * self.h)
            y[0:4] = struct.pack("<I", seq)
            self.mm[off + 64: off + 64 + self.w * self.h] = bytes(y)
            self.mm[off + 64 + self.w * self.h: off + 64 + self.data_bytes] = b"\x80" * (self.w * self.h // 2)
            struct.pack_into("<Q", self.mm, off, sl + 2)
            struct.pack_into("<Q", self.mm, 40, seq)
            self.complete += 1
            if seq == 1:
                emit("first_frame", w=self.w, h=self.h, pts=now())
            if time.monotonic() - last_stats >= 1.0:
                last_stats = time.monotonic()
                emit("stats", complete=self.complete, idle=0, other=0, audio_frames=0, audio_zero=False, shm_skipped=0)
                self.complete = 0
            nxt += period
            delay = nxt - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                nxt = time.monotonic()


class Audio(threading.Thread):
    def __init__(self, fifo):
        super().__init__(daemon=True)
        self.fifo = fifo
        self.stop = threading.Event()

    def run(self):
        fd = os.open(self.fifo, os.O_WRONLY)
        n = 480
        phase = 0
        period = n / 48000.0
        nxt = time.monotonic()
        count = 0
        try:
            while not self.stop.is_set():
                samples = bytearray()
                for i in range(n):
                    v = int(12000 * math.sin(2 * math.pi * 1000 * (phase + i) / 48000.0))
                    samples += struct.pack("<hh", v, v)
                ts = now()
                if os.environ.get("FAKE_AUDIO_GARBAGE") and count == 5:
                    os.write(fd, b"\x01\x02garbage\x00")
                os.write(fd, struct.pack("<IdI", 0x5541434D, ts, n) + bytes(samples))
                phase += n
                count += 1
                nxt += period
                delay = nxt - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
        except OSError:
            pass
        finally:
            os.close(fd)


def main():
    video = audio = None
    emit("ready", version="fake", disclaimed="--disclaim" in sys.argv)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        cmd = next((msg[k] for k in CMD_KEYS if k in msg), None)
        extra = {"id": msg["id"]} if "id" in msg else {}
        if cmd == "check_permission" or cmd == "request_permission":
            emit("permission", screen=True, audio="granted", **extra)
        elif cmd == "list_windows":
            pid = msg.get("pid", 0)
            emit("windows", windows=[
                {"window_id": 11, "pid": pid, "title": "small", "frame": [0, 0, 10, 10], "on_screen": True, "layer": 0},
                {"window_id": 4242, "pid": pid, "title": "main", "frame": [0, 0, 800, 600], "on_screen": True, "layer": 0},
                {"window_id": 12, "pid": pid, "title": "off", "frame": [0, 0, 5000, 5000], "on_screen": False, "layer": 0},
                {"window_id": 13, "pid": pid, "title": "menu", "frame": [0, 0, 5000, 5000], "on_screen": True, "layer": 25},
            ], **extra)
        elif cmd == "start_video":
            video = Video(msg["shm"], msg["width"], msg["height"], msg["fps"], msg.get("id"))
            sys.stderr.write(f"start_video crop={msg['crop']} fps={msg['fps']}\n")
            sys.stderr.flush()
            emit("video_started", recv_t=time.time(), crop=msg["crop"], **extra)
            video.start()
        elif cmd == "update_video":
            sys.stderr.write(f"update_video crop={msg['crop']}\n")
            sys.stderr.flush()
            emit("video_updated", crop=msg["crop"], **extra)
        elif cmd == "stop_video":
            if video:
                video.stop.set()
                video.join(2)
                video = None
            emit("video_stopped", **extra)
        elif cmd == "start_audio":
            audio = Audio(msg["fifo"])
            sys.stderr.write(f"start_audio tree_pid={msg['tree_pid']} mute={msg['mute']}\n")
            sys.stderr.flush()
            audio.start()
            emit("audio_started", pids=[msg["tree_pid"]], device="fake", device_rate=48000, **extra)
        elif cmd == "stop_audio":
            if audio:
                audio.stop.set()
                audio.join(2)
                audio = None
            emit("audio_stopped", **extra)
        elif cmd == "quit":
            emit("bye", **extra)
            break
        else:
            emit("error", where="cmd", msg=f"unknown command {cmd}", code=0, fatal=False, **extra)
    for t in (video, audio):
        if t:
            t.stop.set()
    os._exit(0)


if __name__ == "__main__":
    main()
