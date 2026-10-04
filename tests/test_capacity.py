import os
import shutil
import tempfile
import unittest
from unittest import mock

from multicapture import capacity
from multicapture.capacity import Measurement

GB = 2 ** 30
PIXELS_1080P30 = 1920 * 1080 * 30


def meas(fps1080=225.0, session_cap=None, encoder="libx264"):
    return Measurement(pixels_per_sec=fps1080 * 1920 * 1080, session_cap=session_cap, encoder=encoder,
                       pix_fmt="nv12", ffmpeg_version="ffmpeg version test", measured_at="2026-01-01T00:00:00")


class Slot:
    def __init__(self, w, h):
        self.width, self.height = w, h


class LimitTests(unittest.TestCase):
    def test_speed_bound(self):
        # 0.85 * 225 / 30 = 6.375 -> 6
        self.assertEqual(capacity.limit_for(meas(225), 1920, 1080, 30, 64 * GB), (6, "エンコーダの処理能力"))

    def test_speed_bound_depends_on_size_and_fps(self):
        self.assertEqual(capacity.limit_for(meas(225), 1280, 720, 30, 64 * GB)[0], 14)
        self.assertEqual(capacity.limit_for(meas(225), 1920, 1080, 60, 64 * GB)[0], 3)

    def test_session_bound(self):
        self.assertEqual(capacity.limit_for(meas(2000, session_cap=5), 1920, 1080, 30, 64 * GB), (5, "エンコーダのセッション数"))

    def test_nvenc_default_cap(self):
        self.assertEqual(capacity.limit_for(meas(2000, encoder="h264_nvenc"), 1920, 1080, 30, 64 * GB),
                         (12, "エンコーダのセッション数"))
        self.assertEqual(capacity.limit_for(meas(2000, session_cap=8, encoder="h264_nvenc"), 1920, 1080, 30, 64 * GB)[0], 8)

    def test_memory_bound(self):
        self.assertEqual(capacity.limit_for(meas(2000), 1920, 1080, 30, 8 * GB), (4, "メモリ"))
        self.assertEqual(capacity.limit_for(meas(2000), 1920, 1080, 30, int(12.5 * GB))[0], 8)

    def test_abs_max(self):
        self.assertEqual(capacity.limit_for(meas(5000), 1920, 1080, 30, 64 * GB), (16, "上限16"))

    def test_minimum_one(self):
        self.assertEqual(capacity.limit_for(meas(10), 1920, 1080, 30, 64 * GB)[0], 1)
        self.assertEqual(capacity.limit_for(meas(5000), 1920, 1080, 30, 2 * GB)[0], 1)

    def test_page_load(self):
        m = meas(225)
        slots = [Slot(1920, 1080)] * 3
        self.assertAlmostEqual(capacity.page_load(m, slots, 30), 3 * PIXELS_1080P30 / (0.85 * m.pixels_per_sec))
        self.assertLess(capacity.page_load(m, slots, 30), 1)
        self.assertGreater(capacity.page_load(m, [Slot(1920, 1080)] * 7, 30), 1)
        self.assertEqual(capacity.page_load(m, [], 30), 0)


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = mock.patch.object(capacity, "data_dir", lambda: self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)
        capacity._version_cache["ff"] = "ffmpeg version test"

    def test_round_trip(self):
        m = meas(200, session_cap=12, encoder="h264_nvenc")
        capacity.save(m, "ff")
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "capacity.json")))
        self.assertEqual(capacity.load("ff", "h264_nvenc"), m)
        self.assertIsNone(capacity.load("ff", "libx264"))

    def test_key_includes_ffmpeg_version(self):
        capacity.save(meas(200), "ff")
        capacity._version_cache["ff"] = "ffmpeg version other"
        self.assertIsNone(capacity.load("ff", "libx264"))

    def test_corrupt_cache(self):
        with open(os.path.join(self.tmp, "capacity.json"), "w") as f:
            f.write("{broken")
        self.assertIsNone(capacity.load("ff", "libx264"))
        capacity.save(meas(200), "ff")
        self.assertIsNotNone(capacity.load("ff", "libx264"))

    def test_note_session_failure(self):
        self.assertIsNone(capacity.note_session_failure("ff", "h264_nvenc", 3))
        capacity.save(meas(2000, session_cap=12, encoder="h264_nvenc"), "ff")
        capacity.note_session_failure("ff", "h264_nvenc", 8)
        self.assertEqual(capacity.load("ff", "h264_nvenc").session_cap, 8)
        capacity.note_session_failure("ff", "h264_nvenc", 10)  # never raises the cap
        self.assertEqual(capacity.load("ff", "h264_nvenc").session_cap, 8)
        capacity.note_session_failure("ff", "h264_nvenc", 0)
        self.assertEqual(capacity.load("ff", "h264_nvenc").session_cap, 1)

    def test_note_session_failure_without_cap(self):
        capacity.save(meas(2000, encoder="h264_qsv"), "ff")
        capacity.note_session_failure("ff", "h264_qsv", 4)
        self.assertEqual(capacity.load("ff", "h264_qsv").session_cap, 4)

    def test_log_has_session_error(self):
        path = os.path.join(self.tmp, "x.log")
        for text, encoder, expected in (
            ("[h264_nvenc] OpenEncodeSessionEx failed: incompatible client key (21)", "h264_nvenc", True),
            ("[h264_videotoolbox] Error: cannot create compression session: -12915", "h264_videotoolbox", True),
            ("[h264_videotoolbox] OpenEncodeSessionEx", "h264_videotoolbox", False),
            ("frame= 10", "h264_nvenc", False),
        ):
            with open(path, "w") as f:
                f.write(text)
            self.assertEqual(capacity.log_has_session_error(path, encoder), expected, text)
        self.assertFalse(capacity.log_has_session_error(os.path.join(self.tmp, "none.log"), "h264_nvenc"))


if __name__ == "__main__":
    unittest.main()
