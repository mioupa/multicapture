import json
import math
import os
import random
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

if sys.platform == "darwin":
    from multicapture import ffmpeg
    from multicapture import platform as osp
    from multicapture.platform.mac import capture, helper

HERE = os.path.dirname(os.path.abspath(__file__))
FAKE = os.path.join(HERE, "fake_mc_capture.py")
FFMPEG = None
if sys.platform == "darwin":
    FFMPEG = ffmpeg.find_ffmpeg()

_tmp_data = None
_patches = []


def setUpModule():
    global _tmp_data
    if sys.platform != "darwin":
        return
    _tmp_data = tempfile.mkdtemp(prefix="mc-test-data-")
    p = mock.patch.object(osp, "default_data_dir", lambda app_dir: _tmp_data)
    p.start()
    _patches.append(p)
    p = mock.patch.dict(os.environ, {"MULTICAPTURE_HELPER": FAKE})
    p.start()
    _patches.append(p)


def tearDownModule():
    for p in _patches:
        p.stop()
    if _tmp_data:
        shutil.rmtree(_tmp_data, ignore_errors=True)


class StubBrowser:
    """Just enough BrowserWindow for the capture adapters and SlotRecorder."""

    def __init__(self, width=320, height=180, offset=(0, 0)):
        self.pid = os.getpid()
        self.width, self.height, self.offset = width, height, offset
        self.helper_session = None

    def find_window_id(self, timeout=15.0):
        return helper.pick_window(helper.list_windows(self.pid))

    def fit_content(self, w, h, attempts=3):
        return w, h

    def capture_offset(self):
        return self.offset

    def restore_if_minimized(self):
        pass

    def alive(self):
        return True


class PyRingWriter:
    """Writes the ring file like the helper (offsets from the slot start)."""

    def __init__(self, path, w, h, slots=6):
        self.w, self.h, self.slots = w, h, slots
        self.data_bytes = w * h * 3 // 2
        self.slot_size = 64 + self.data_bytes
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600)
        os.ftruncate(fd, 64 + slots * self.slot_size)
        import mmap
        self.mm = mmap.mmap(fd, 0)
        os.close(fd)
        capture.FILE_HDR.pack_into(self.mm, 0, b"MCRING01", 1, slots, self.slot_size, 64, 0, w, h)
        self.seq = 0
        self.frames = {}

    def write(self, pts, payload=None):
        self.seq += 1
        off = 64 + (self.seq % self.slots) * self.slot_size
        payload = payload or random.randbytes(self.data_bytes)
        self.frames[self.seq] = payload
        sl = struct.unpack_from("<Q", self.mm, off)[0]
        struct.pack_into("<Q", self.mm, off, sl + 1)
        capture.SLOT_HDR.pack_into(self.mm, off, sl + 1, self.seq, pts, self.w, self.h, self.w, self.w, 64,
                                   64 + self.w * self.h, self.data_bytes)
        self.mm[off + 64: off + 64 + self.data_bytes] = payload
        struct.pack_into("<Q", self.mm, off, sl + 2)
        struct.pack_into("<Q", self.mm, 40, self.seq)


class Reference:
    """wgc.WindowCapture's ring bookkeeping, fed one frame at a time."""

    def __init__(self):
        self.ring_times = [float("-inf")] * 6
        self.ring_seq = [-1] * 6
        self.seq = 0
        self.head = 0

    def add(self, slot, t):
        self.seq += 1
        self.ring_times[slot] = t
        self.ring_seq[slot] = self.seq
        self.head = slot

    def slot_of_seq(self, seq):
        for slot in range(6):
            if self.ring_seq[slot] == seq:
                return slot
        return None

    def oldest_seq(self):
        valid = [s for s in self.ring_seq if s >= 0]
        return min(valid) if valid else None

    def first_seq_after(self, wall):
        c = [(self.ring_seq[i], self.ring_times[i]) for i in range(6) if self.ring_seq[i] >= 0 and self.ring_times[i] >= wall]
        return min(c)[0] if c else None

    def slot_at(self, wall):
        best = None
        for slot in range(6):
            if self.ring_seq[slot] >= 0 and self.ring_times[slot] <= wall:
                if best is None or self.ring_times[slot] > self.ring_times[best]:
                    best = slot
        if best is not None:
            return best
        valid = [s for s in range(6) if self.ring_seq[s] >= 0]
        return min(valid, key=lambda s: self.ring_times[s]) if valid else self.head


@unittest.skipUnless(sys.platform == "darwin", "macOS only")
class RingSemanticsTests(unittest.TestCase):
    W, H = 16, 8

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "ring")
        self.writer = PyRingWriter(self.path, self.W, self.H)
        self.cap = capture.MacWindowCapture(StubBrowser())
        self.cap.set_output(self.W, self.H)
        self.cap.ring = capture.RingReader(self.path)
        self.addCleanup(self.cap.ring.close)
        self.ref = Reference()

    def feed(self, count, t0, step):
        for i in range(count):
            self.writer.write(t0 + i * step)

    def compare(self, queries):
        c, r = self.cap, self.ref
        self.assertEqual(c.seq, r.seq)
        self.assertEqual(c.head, r.head)
        self.assertEqual(list(c.ring_seq), r.ring_seq)
        self.assertEqual(list(c.ring_times), r.ring_times)
        self.assertEqual(c.oldest_seq(), r.oldest_seq())
        for q in queries:
            self.assertEqual(c.slot_at(q), r.slot_at(q))
            self.assertEqual(c.first_seq_after(q), r.first_seq_after(q))
        for s in range(0, r.seq + 3):
            self.assertEqual(c.slot_of_seq(s), r.slot_of_seq(s))

    def test_empty(self):
        self.assertFalse(self.cap.update())
        self.compare([0, 100])
        self.assertIsNone(self.cap.oldest_seq())

    def test_matches_reference_incremental(self):
        t = 1000.0
        for burst in (1, 1, 2, 3, 1, 5, 4, 2):
            for _ in range(burst):
                self.writer.write(t)
                self.ref.add(self.writer.seq % 6, t)
                t += 1 / 30
            self.assertTrue(self.cap.update())
            self.compare([t - 1, t - 0.1, t - 1 / 60, t, t + 1, 0])
            self.assertFalse(self.cap.update())

    def test_burst_larger_than_ring_counts_lost(self):
        t = 50.0
        for _ in range(3):
            self.writer.write(t)
            self.ref.add(self.writer.seq % 6, t)
            t += 0.03
        self.cap.update()
        for _ in range(10):
            self.writer.write(t)
            if self.writer.seq > 7:
                self.ref.add(self.writer.seq % 6, t)
            t += 0.03
        self.assertTrue(self.cap.update())
        self.assertEqual(self.cap.lost, 4)
        # consecutive python-side numbering: 3 old + the 6 surviving frames
        self.assertEqual(self.cap.seq, 9)
        self.assertEqual(sorted(s for s in self.cap.ring_seq if s >= 0), [4, 5, 6, 7, 8, 9])
        self.assertEqual(self.cap.oldest_seq(), 4)

    def test_write_slot_bytes(self):
        t = 10.0
        for _ in range(9):
            self.writer.write(t)
            t += 0.03
        self.cap.update()
        for seq in range(4, 10):
            slot = self.cap.slot_of_seq(self.cap.seq - (9 - seq))
            got = []
            self.cap.write_slot(got.append, slot)
            self.assertEqual(bytes(got[0]), self.writer.frames[seq])
        self.assertEqual(self.cap.torn, 0)

    def test_write_slot_torn_is_counted_and_still_written(self):
        self.writer.write(5.0)
        self.cap.update()
        off = 64 + (1 % 6) * self.writer.slot_size
        struct.pack_into("<Q", self.writer.mm, off, 3)   # odd: "being written"
        got = []
        self.cap.write_slot(got.append, 1)
        self.assertEqual(len(got[0]), self.W * self.H * 3 // 2)
        self.assertEqual(self.cap.torn_written, 1)


@unittest.skipUnless(sys.platform == "darwin", "macOS only")
class FfmpegMacArgsTests(unittest.TestCase):
    def build(self, encoder, pix_fmt):
        return ffmpeg.build_command("ffmpeg", 1920, 1080, 30, "/tmp/a.fifo", 48000, 2, 1920, 1080, encoder, "o.mp4",
                                    pix_fmt=pix_fmt)

    def test_nv12_input_is_tagged_bt709_tv(self):
        cmd = self.build("libx264", "nv12")
        i = cmd.index("-i")
        tags = ["-color_range", "tv", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"]
        j = cmd.index("-color_range")
        self.assertEqual(cmd[j:j + 8], tags)
        self.assertLess(j, i)
        self.assertEqual(cmd[cmd.index("-pix_fmt", i) + 1], "yuv420p")

    def test_bgra_input_has_no_tags(self):
        self.assertNotIn("-color_range", self.build("libx264", "bgra"))

    def test_videotoolbox_outputs_nv12_others_yuv420p(self):
        vt = self.build("h264_videotoolbox", "nv12")
        self.assertEqual(vt[vt.index("-pix_fmt", vt.index("-i")) + 1], "nv12")
        for enc in ("libx264", "h264_nvenc", "h264_amf", "h264_qsv"):
            c = self.build(enc, "bgra")
            self.assertEqual(c[c.index("-pix_fmt", c.index("-i")) + 1], "yuv420p")


@unittest.skipUnless(sys.platform == "darwin", "macOS only")
class FifoParserTests(unittest.TestCase):
    @staticmethod
    def record(ts, frames, fill=1):
        return struct.pack("<IdI", capture.AUDIO_MAGIC, ts, frames) + bytes([fill]) * (frames * 4)

    def test_records_split_across_chunks(self):
        stream = b"".join(self.record(1.0 + i * 0.01, 480, i) for i in range(5))
        parser = capture.FifoParser()
        out = []
        for i in range(0, len(stream), 777):
            out += parser.feed(stream[i:i + 777])
        self.assertEqual([round(t, 2) for _, t in out], [1.0, 1.01, 1.02, 1.03, 1.04])
        self.assertTrue(all(len(d) == 480 * 4 for d, _ in out))
        self.assertEqual(out[3][0][0], 3)
        self.assertEqual(parser.resyncs, 0)

    def test_resync_after_garbage(self):
        stream = self.record(1.0, 100) + b"\x01\x02garbage\x00\xff" + self.record(2.0, 100) + b"xx" + self.record(3.0, 50)
        parser = capture.FifoParser()
        out = parser.feed(stream)
        self.assertEqual([t for _, t in out], [1.0, 2.0, 3.0])
        self.assertGreaterEqual(parser.resyncs, 1)

    def test_garbage_split_across_feeds_and_huge_frame_count(self):
        bad = struct.pack("<IdI", capture.AUDIO_MAGIC, 1.0, 0x7FFFFFFF)
        stream = bad + self.record(2.0, 10)
        parser = capture.FifoParser()
        out = parser.feed(stream[:5]) + parser.feed(stream[5:20]) + parser.feed(stream[20:])
        self.assertEqual([t for _, t in out], [2.0])


@unittest.skipUnless(sys.platform == "darwin", "macOS only")
class SessionTests(unittest.TestCase):
    def test_helper_found_via_env(self):
        self.assertEqual(helper.helper_command()[0], FAKE)

    def test_reply_routing_by_id_under_concurrency(self):
        s = helper.HelperSession("test-routing.log").start()
        self.addCleanup(s.close)
        results, errors = {}, []

        def worker(pid):
            try:
                for _ in range(10):
                    ws = s.request("list_windows", pid=pid)["windows"]
                    if {w["pid"] for w in ws} != {pid}:
                        errors.append((pid, ws))
            except Exception as exc:
                errors.append(exc)
            results[pid] = True

        threads = [threading.Thread(target=worker, args=(1000 + i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)

    def test_error_reply_raises_and_state_is_kept(self):
        s = helper.HelperSession("test-error.log").start()
        self.addCleanup(s.close)
        with self.assertRaises(helper.HelperError):
            s.request("no_such_command")
        self.assertEqual(s.errors[-1]["where"], "cmd")

    def test_pick_window(self):
        s = helper.HelperSession("test-pick.log").start()
        self.addCleanup(s.close)
        self.assertEqual(helper.pick_window(s.request("list_windows", pid=5)["windows"]), 4242)

    def test_start_video_stagger(self):
        sessions = [helper.HelperSession(f"test-stagger{i}.log").start() for i in range(3)]
        self.addCleanup(lambda: [s.close() for s in sessions])
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        recv = {}

        def go(i):
            ev = sessions[i].start_video(1, 64, 36, [0, 0, 64, 36], 30, os.path.join(tmp, f"r{i}"))
            recv[i] = ev["recv_t"]

        threads = [threading.Thread(target=go, args=(i,)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        times = sorted(recv.values())
        self.assertEqual(len(times), 3)
        self.assertGreaterEqual(times[1] - times[0], 0.45)
        self.assertGreaterEqual(times[2] - times[1], 0.45)

    def test_close_terminates_process(self):
        s = helper.HelperSession("test-close.log").start()
        proc = s._proc
        s.close()
        self.assertIsNotNone(proc.poll())
        self.assertFalse(s.alive)

    def test_shared_session_closed_by_last_user(self):
        bw = StubBrowser()
        a = helper.acquire_session(bw, "test-shared.log")
        b = helper.acquire_session(bw, "test-shared.log")
        self.assertIs(a, b)
        helper.release_session(bw, a)
        self.assertTrue(a.alive)
        helper.release_session(bw, b)
        self.assertFalse(a.alive)
        self.assertIsNone(bw.helper_session)


@unittest.skipUnless(sys.platform == "darwin", "macOS only")
class CaptureWithFakeHelperTests(unittest.TestCase):
    def test_video_and_audio_share_one_helper(self):
        bw = StubBrowser()
        cap = osp.open_window_capture(bw)
        cap.fps = 30
        cap.start()
        cap.set_output(320, 180)
        cap.set_region(0, 32)
        aud = osp.open_audio_capture(bw, 48000, 2, mute=True)
        aud.start()
        try:
            self.assertIs(cap.session, aud.session)
            end = time.monotonic() + 1.5
            while time.monotonic() < end:
                cap.update()
                time.sleep(0.004)
            self.assertGreater(cap.seq, 25)
            got = []
            cap.write_slot(got.append, cap.head)
            self.assertEqual(len(got[0]), 320 * 180 * 3 // 2)
            self.assertEqual(struct.unpack_from("<I", got[0], 0)[0] % 256, got[0][100])
            cap.set_region(0, 40)   # moved offset -> update_video
            time.sleep(0.3)
            with open(os.path.join(_tmp_data, "logs", "mc-capture_%d.log" % bw.pid), errors="replace") as f:
                log = f.read()
            self.assertIn("update_video crop=[0, 40, 320, 180]", log)
            self.assertIn("mute=mutedWhenTapped", log)
            self.assertIn(f"tree_pid={bw.pid}", log)
            chunks = aud.read_timed()
            self.assertGreater(len(chunks), 50)
        finally:
            aud.close()
            self.assertTrue(cap.session.alive)
            ring_dir = cap._tmpdir
            cap.close()
        self.assertFalse(os.path.exists(ring_dir))
        self.assertIsNone(bw.helper_session)

    def audio_run(self, garbage):
        env = {"FAKE_AUDIO_GARBAGE": "1"} if garbage else {}
        with mock.patch.dict(os.environ, env):
            bw = StubBrowser()
            aud = osp.open_audio_capture(bw, 48000, 2)
            aud.start()
            fifo_dir = aud._dir
            records = []
            try:
                end = time.monotonic() + 1.0
                while time.monotonic() < end:
                    records += aud.read_timed()
                    time.sleep(0.01)
            finally:
                aud.close()
            self.assertFalse(os.path.exists(fifo_dir))
            return records, aud

    def test_audio_timestamps_monotonic_and_tone(self):
        records, _ = self.audio_run(False)
        self.assertGreater(len(records), 60)
        stamps = [t for _, t in records]
        self.assertEqual(stamps, sorted(stamps))
        now = osp.clock.now()
        self.assertLess(abs(now - stamps[-1]), 3.0)
        # consecutive records are 10 ms apart
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        self.assertLess(max(gaps), 0.1)
        data = b"".join(d for d, _ in records[:20])
        samples = struct.unpack("<%dh" % (len(data) // 2), data)
        left = samples[0::2]
        crossings = sum(1 for a, b in zip(left, left[1:]) if a < 0 <= b)
        self.assertAlmostEqual(crossings / (len(left) / 48000.0), 1000, delta=60)

    def test_audio_survives_garbage_in_fifo(self):
        records, aud = self.audio_run(True)
        self.assertGreater(len(records), 60)
        self.assertGreaterEqual(aud.parser.resyncs, 1)
        stamps = [t for _, t in records]
        self.assertEqual(stamps, sorted(stamps))


@unittest.skipUnless(sys.platform == "darwin", "macOS only")
class CleanupTests(unittest.TestCase):
    def spawn(self, flag):
        return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", flag],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def test_kills_only_our_profile_processes(self):
        root = tempfile.mkdtemp()
        other = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        self.addCleanup(shutil.rmtree, other, True)
        mine = self.spawn(f"--user-data-dir={root}/profiles/login")
        mine2 = self.spawn(f"--user-data-dir={root}/work/ab/seg1")
        sibling = self.spawn(f"--user-data-dir={root}-sibling/profiles/x")   # shares only a name prefix
        foreign = self.spawn(f"--user-data-dir={other}/profiles/x")
        procs = [mine, mine2, sibling, foreign]
        self.addCleanup(lambda: [(p.kill(), p.wait()) for p in procs if p.poll() is None])
        time.sleep(0.3)
        killed = osp.cleanup_leftovers(root)
        for p in (mine, mine2):
            self.assertIsNotNone(p.wait(5))
        self.assertIn(mine.pid, killed)
        self.assertIsNone(sibling.poll())
        self.assertIsNone(foreign.poll())
        self.assertNotIn(os.getpid(), killed)

    def test_helper_and_caffeinate_matching_with_fake_rows(self):
        dead = 99999999
        helper_exe = helper.helper_executables()[0]
        rows = [
            (101, 1, f"{helper_exe} --disclaim serve"),            # source-mode argv, orphaned
            (102, 1, f"{helper_exe} serve"),
            (103, 5555, f"{helper_exe} serve"),                    # has a live parent: keep
            (104, 1, "/somewhere/else/mc-capture serve"),         # not our path
            (105, 1, f"caffeinate -d -i -w {dead}"),
            (106, 1, f"caffeinate -d -i -w {os.getpid()}"),      # parent alive: keep
            (107, 1, f"caffeinate -u -t 5 -w {dead}"),            # not our flags
            (108, 1, "/usr/bin/vim"),
        ]
        killed = []
        result = osp.cleanup_leftovers(tempfile.gettempdir() + "/nonexistent-root", rows=rows,
                                       kill=lambda pid, sig: killed.append(pid))
        self.assertEqual(sorted(set(result)), [101, 102, 105])
        self.assertEqual(sorted(set(killed)), [101, 102, 105])


@unittest.skipUnless(sys.platform == "darwin" and FFMPEG, "macOS with ffmpeg only")
class RecorderEndToEndTests(unittest.TestCase):
    def test_slot_recorder_with_fake_helper(self):
        from multicapture.config import Slot
        from multicapture.recorder import SlotRecorder

        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        out = os.path.join(tmp, "out.mp4")

        class S:
            fps = 30
            output_dir = tmp

        statuses = []
        bw = StubBrowser(320, 180, offset=(0, 0))
        rec = SlotRecorder(0, Slot("t", "about:blank", 320, 180), bw, S, FFMPEG, "libx264",
                           lambda i, text: statuses.append(text), output_path=out)
        rec.start()
        self.assertTrue(rec.ready.wait(20), rec.error)
        time.sleep(5.0)
        rec.stop()
        rec.join(60)
        self.assertIsNone(rec.error)
        self.assertGreater(rec.frames_written, 100)

        probe = json.loads(subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", out],
            capture_output=True, text=True, check=True).stdout)
        kinds = {s["codec_type"]: s for s in probe["streams"]}
        self.assertEqual(kinds["video"]["codec_name"], "h264")
        self.assertEqual(kinds["video"]["pix_fmt"], "yuv420p")
        self.assertEqual((kinds["video"]["width"], kinds["video"]["height"]), (320, 180))
        self.assertIn("audio", kinds)
        self.assertAlmostEqual(float(probe["format"]["duration"]), 5.0, delta=1.0)
        self.assertAlmostEqual(float(kinds["audio"].get("duration", probe["format"]["duration"])), 5.0, delta=1.0)
        vol = subprocess.run([FFMPEG, "-hide_banner", "-i", out, "-vn", "-af", "volumedetect", "-f", "null", "-"],
                             capture_output=True, text=True).stderr
        mean = float(vol.split("mean_volume:")[1].split("dB")[0])
        self.assertGreater(mean, -30.0)
        self.assertIsNone(bw.helper_session)


if __name__ == "__main__":
    unittest.main()
