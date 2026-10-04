import os
import shutil
import subprocess
import tempfile
import unittest

from multicapture import ffmpeg

FFMPEG = ffmpeg.find_ffmpeg() or shutil.which("ffmpeg")
FPS = 30


@unittest.skipUnless(FFMPEG, "ffmpeg not available")
class FindJoinTests(unittest.TestCase):
    """Clip B is the content of clip A starting at frame K, so the join point is K / FPS in A."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def make(self, name, start_frame, frames):
        path = os.path.join(self.tmp, name)
        # testsrc2 has a frame counter and moving elements, so every frame differs
        vf = f"select='gte(n\\,{start_frame})',setpts=N/{FPS}/TB" if start_frame else f"setpts=N/{FPS}/TB"
        subprocess.run(
            [FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size=320x180:rate={FPS}",
             "-vf", vf, "-frames:v", str(frames), "-c:v", "libx264", "-preset", "ultrafast", "-crf", "10", "-g", "10", path],
            check=True, capture_output=True, timeout=60)
        return path

    def test_finds_known_overlap(self):
        total_a = 120  # 4 s
        for k in (87, 100, 110):  # within the last 1.2 s of A
            with self.subTest(k=k):
                a = self.make(f"a{k}.mp4", 0, total_a)
                b = self.make(f"b{k}.mp4", k, 60)
                found = ffmpeg.find_join(FFMPEG, a, total_a / FPS, b, 0.0, FPS, k / FPS)
                self.assertIsNotNone(found)
                self.assertLessEqual(abs(found - k / FPS), 1 / FPS + 1e-6)

    def test_nominal_hint_breaks_ties(self):
        a = self.make("a.mp4", 0, 120)
        b = self.make("b.mp4", 90, 40)
        found = ffmpeg.find_join(FFMPEG, a, 4.0, b, 0.0, FPS, 3.05)
        self.assertLessEqual(abs(found - 3.0), 1 / FPS + 1e-6)


if __name__ == "__main__":
    unittest.main()
