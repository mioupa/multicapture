import unittest

from multicapture.recorder import (
    AUDIO_DELAY_SECONDS, AUDIO_MAX_GAP_SECONDS, BLOCK_ALIGN, SAMPLE_RATE, AudioPlacer, SlotRecorder,
)

T0 = 100.0


def chunk(frames, fill=1):
    return bytes([fill]) * (frames * BLOCK_ALIGN)


class Harness:
    """AudioPlacer wired to the real SlotRecorder timeline methods."""

    def __init__(self, sequential=False, pauses=()):
        self.rec = SlotRecorder(0, None, None, None, None, None, lambda *a: None)
        self.rec._t0 = T0
        self.rec._pauses = [list(p) for p in pauses]
        self.out = []
        self.placer = AudioPlacer(self.rec.position_of, self.rec.pause_started_after, T0, sequential, self.out.append)

    @property
    def emitted_frames(self):
        return sum(len(b) for b in self.out) // BLOCK_ALIGN

    def silence_frames(self):
        return sum(len(b) for b in self.out if not any(b)) // BLOCK_ALIGN


class PlaceTests(unittest.TestCase):
    def test_in_order_chunks_are_contiguous(self):
        h = Harness()
        for k in range(3):
            h.placer.place(chunk(480), T0 + k * 0.01)
        self.assertEqual(h.placer.written, 1440)
        self.assertEqual(h.emitted_frames, 1440)
        self.assertEqual(h.silence_frames(), 0)

    def test_gap_inserts_silence(self):
        h = Harness()
        h.placer.place(chunk(480), T0)
        h.placer.place(chunk(480), T0 + 0.05)  # target 2400, written 480 -> 1920 frames of silence
        self.assertEqual(h.out[1], bytes(1920 * BLOCK_ALIGN))
        self.assertEqual(h.placer.written, 2880)

    def test_partial_overlap_is_trimmed(self):
        h = Harness()
        h.placer.place(chunk(480, 1), T0)
        h.placer.place(chunk(480, 2), T0 + 0.005)  # target 240 < written 480 -> first 240 frames dropped
        self.assertEqual(len(h.out[1]), 240 * BLOCK_ALIGN)
        self.assertEqual(h.placer.written, 720)

    def test_fully_late_chunk_is_dropped(self):
        h = Harness()
        h.placer.place(chunk(480), T0)
        h.placer.place(chunk(480), T0)
        self.assertEqual(len(h.out), 1)
        self.assertEqual(h.placer.written, 480)

    def test_chunk_before_t0_is_skipped(self):
        h = Harness()
        stamp = T0 - 0.005
        skip = int((T0 - stamp) * SAMPLE_RATE) + 1
        h.placer.place(chunk(480), stamp)
        self.assertEqual(h.emitted_frames - h.silence_frames(), 480 - skip)
        self.assertLessEqual(h.placer.written, 480 - skip + 2)
        # entirely before t0
        h2 = Harness()
        h2.placer.place(chunk(480), T0 - 1.0)
        self.assertEqual(h2.out, [])
        self.assertEqual(h2.placer.written, 0)

    def test_paused_chunk_inside_pause_is_dropped(self):
        h = Harness(pauses=[[T0 + 0.5, T0 + 1.0]])
        h.placer.place(chunk(480), T0 + 0.6)
        self.assertEqual(h.out, [])
        self.assertEqual(h.placer.written, 0)

    def test_chunk_straddling_pause_start_is_cut(self):
        h = Harness(pauses=[[T0 + 0.5, T0 + 1.0]])
        h.placer.place(chunk(480), T0 + 0.495)
        # silence up to 0.495 s (23760 frames), then 240 frames kept before the pause begins
        self.assertEqual(h.placer.written, 24000)
        self.assertEqual(h.silence_frames(), 23760)
        # first chunk after the pause continues on the paused-out timeline (0.5 s)
        h.placer.place(chunk(480), T0 + 1.0)
        self.assertEqual(h.placer.written, 24480)
        self.assertEqual(h.silence_frames(), 23760)

    def test_chunk_ending_at_pause_start_with_zero_keep_returns(self):
        h = Harness(pauses=[[T0 + 0.5, T0 + 1.0]])
        h.placer.place(chunk(480), T0 + 0.4999999)  # cut is <1 frame away -> keep == 0
        # only the lead-in silence (target 24000) is emitted; no audio data, and place() returns
        self.assertEqual(h.emitted_frames, 24000)
        self.assertEqual(h.silence_frames(), 24000)
        self.assertEqual(h.placer.written, 24000)

    def test_gap_clamp(self):
        limit = int(AUDIO_MAX_GAP_SECONDS * SAMPLE_RATE)
        h = Harness()
        h.placer.place(chunk(480), T0 + AUDIO_MAX_GAP_SECONDS)  # gap == limit: still filled
        self.assertEqual(h.silence_frames(), limit)
        self.assertEqual(h.placer.written, limit + 480)
        h = Harness()
        h.placer.place(chunk(480), T0 + AUDIO_MAX_GAP_SECONDS + 1)  # gap > limit: not filled
        self.assertEqual(h.silence_frames(), 0)
        self.assertEqual(h.placer.written, 480)

    def test_sequential_shift(self):
        self.assertLess(AUDIO_DELAY_SECONDS, 0)
        # stamped exactly at t0: with the shift the chunk starts before t0 and is dropped
        h = Harness(sequential=True)
        h.placer.place(chunk(480), T0)
        self.assertEqual(h.out, [])
        # a chunk stamped t0 + 25 ms lands at position 0
        h = Harness(sequential=True)
        h.placer.place(chunk(480), T0 - AUDIO_DELAY_SECONDS)
        self.assertEqual(h.placer.written, 480)
        self.assertEqual(h.silence_frames(), 0)
        # non-sequential places the same stamp 25 ms later
        h = Harness(sequential=False)
        h.placer.place(chunk(480), T0 - AUDIO_DELAY_SECONDS)
        self.assertEqual(h.silence_frames(), 1200)


if __name__ == "__main__":
    unittest.main()
