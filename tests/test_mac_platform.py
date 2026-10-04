import os
import sys
import threading
import time
import unittest

if sys.platform == "darwin":
    from multicapture import platform as osp


@unittest.skipUnless(sys.platform == "darwin", "macOS only")
class MacPlatformTests(unittest.TestCase):
    def test_clock_monotonic_and_matches_perf_counter(self):
        a, p = osp.clock.now(), time.perf_counter()
        time.sleep(0.2)
        b, q = osp.clock.now(), time.perf_counter()
        self.assertGreater(b, a)
        self.assertAlmostEqual(b - a, q - p, delta=0.05)
        last = osp.clock.now()
        for _ in range(1000):
            now = osp.clock.now()
            self.assertGreaterEqual(now, last)
            last = now

    def test_audio_pipe_round_trip(self):
        pipe = osp.AudioPipe()
        try:
            self.assertTrue(os.path.exists(pipe.path))
            self.assertEqual(os.stat(pipe.path).st_mode & 0o777, 0o600)
            payload = os.urandom(1 << 20)
            received = bytearray()

            def reader():
                fd = os.open(pipe.path, os.O_RDONLY)
                with os.fdopen(fd, "rb") as f:
                    while True:
                        data = f.read(65536)
                        if not data:
                            return
                        received.extend(data)

            t = threading.Thread(target=reader)
            t.start()
            pipe.connect()
            pipe.write(payload)
            pipe.close()
            t.join(10)
            self.assertFalse(t.is_alive())
            self.assertEqual(bytes(received), payload)
            self.assertFalse(os.path.exists(pipe.path))
        finally:
            pipe.close()

    def test_close_unblocks_waiting_connect(self):
        pipe = osp.AudioPipe()
        result = []

        def connect():
            try:
                pipe.connect()
                result.append("connected")
            except OSError as exc:
                result.append(exc)

        t = threading.Thread(target=connect)
        t.start()
        time.sleep(0.3)
        self.assertTrue(t.is_alive())
        start = time.monotonic()
        pipe.close()
        t.join(3)
        self.assertFalse(t.is_alive())
        self.assertLess(time.monotonic() - start, 2.0)
        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], OSError)
        self.assertFalse(os.path.exists(os.path.dirname(pipe.path)))

    def test_write_after_reader_gone_raises_broken_pipe(self):
        pipe = osp.AudioPipe()
        try:
            def reader():
                fd = os.open(pipe.path, os.O_RDONLY)
                os.read(fd, 10)
                os.close(fd)

            t = threading.Thread(target=reader)
            t.start()
            pipe.connect()
            pipe.write(b"x" * 100)
            t.join(5)
            with self.assertRaises(BrokenPipeError):
                for _ in range(100):
                    pipe.write(b"x" * 65536)
        finally:
            pipe.close()

    def test_default_dirs(self):
        home = os.path.expanduser("~")
        self.assertEqual(osp.default_output_dir(), os.path.join(home, "Movies", "MultiCapture"))
        self.assertEqual(osp.default_data_dir("/nonexistent"), os.path.join(home, "Library", "Application Support", "MultiCapture"))

    def test_misc(self):
        self.assertIn("/opt/homebrew/bin/ffmpeg", osp.ffmpeg_candidates("/x"))
        self.assertGreater(osp.physical_memory_bytes(), 1 << 30)
        w, h = osp.primary_screen_size()
        self.assertGreater(w, 100)
        self.assertGreater(h, 100)
        self.assertEqual(osp.popen_kwargs(), {})
        self.assertTrue(osp.DISPLAY_ALWAYS_ON and osp.MUTE_VIA_CAPTURE and not osp.SCHEDULE_SUPPORTED)

    def test_keep_awake(self):
        ka = osp.KeepAwake("test")
        self.assertTrue(ka.acquire())
        self.assertIsNone(ka._proc.poll())
        proc = ka._proc
        ka.release()
        self.assertIsNotNone(proc.poll())

    def test_available_browsers_finds_chrome(self):
        if not os.path.isdir("/Applications/Google Chrome.app"):
            self.skipTest("Chrome not installed")
        found = osp.available_browsers()
        self.assertTrue(found["chrome"].endswith("Google Chrome.app/Contents/MacOS/Google Chrome"))


if __name__ == "__main__":
    unittest.main()
