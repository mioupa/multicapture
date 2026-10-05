import math
import os
import shutil
import subprocess

from . import platform as osp
from .config import app_dir

ENCODERS = {
    "h264_nvenc": ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "23", "-b:v", "0"],
    "h264_amf": ["-c:v", "h264_amf", "-quality", "balanced", "-rc", "cqp", "-qp_i", "21", "-qp_p", "23"],
    "h264_qsv": ["-c:v", "h264_qsv", "-preset", "medium", "-global_quality", "23"],
    "libx264": ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"],
    # q:v 55 matches x264 crf 23 in SSIM at 1.2-1.3x the bitrate (tools/verify/vt_quality.py)
    "h264_videotoolbox": ["-c:v", "h264_videotoolbox", "-allow_sw", "0", "-q:v", "55"],
}

FORCED_IDR = {
    "h264_nvenc": ["-forced-idr", "1"],
    "h264_amf": ["-forced_idr", "1"],
    "h264_qsv": ["-forced_idr", "1"],
}

ENCODER_LABELS = {
    "h264_nvenc": "NVIDIA NVENC",
    "h264_amf": "AMD AMF",
    "h264_qsv": "Intel Quick Sync",
    "libx264": "CPU (x264)",
    "h264_videotoolbox": "Apple VideoToolbox",
}

_detected = None
JOIN_FADE_SECONDS = 0.003
SIG_SIZE = (64, 36)


def find_ffmpeg():
    for candidate in osp.ffmpeg_candidates(app_dir()):
        if os.path.isfile(candidate):
            return candidate
    return shutil.which("ffmpeg")


def _works(ffmpeg, encoder):
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=black:s=1280x720:r=30",
        "-frames:v", "5", "-pix_fmt", "yuv420p", *ENCODERS[encoder], "-f", "null", "-",
    ]
    try:
        return subprocess.run(cmd, capture_output=True, timeout=20, **osp.popen_kwargs()).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def detect_encoder(ffmpeg, preference="auto"):
    global _detected
    if preference in ENCODERS and preference != "auto":
        return preference
    if _detected is None:
        _detected = next((e for e in osp.HW_ENCODERS if _works(ffmpeg, e)), "libx264")
    return _detected


def _list_line(path):
    return "file '" + path.replace("\\", "/").replace("'", "'\\''") + "'\n"


def _run(cmd, log):
    log.write(" ".join(cmd) + "\n")
    log.flush()
    return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=log, **osp.popen_kwargs()).returncode


def concat(ffmpeg, parts, output, log_path):
    video_list = output + ".video.txt"
    audio_list = output + ".audio.txt"
    temp = [video_list, audio_list]
    try:
        with open(log_path, "w", encoding="utf-8", errors="replace") as log:
            with open(video_list, "w", encoding="utf-8") as vf, open(audio_list, "w", encoding="utf-8") as af:
                for k, (part, inpoint, outpoint) in enumerate(parts):
                    vf.write(_list_line(part))
                    if inpoint:
                        vf.write(f"inpoint {inpoint:.6f}\n")
                    if outpoint:
                        vf.write(f"outpoint {outpoint:.6f}\n")
                    length = (outpoint - (inpoint or 0.0)) if outpoint else None
                    trim = []
                    if inpoint:
                        trim.append(f"start={inpoint:.6f}")
                    if outpoint:
                        trim.append(f"end={outpoint:.6f}")
                    filters = [f"atrim={':'.join(trim)}" if trim else "anull", "asetpts=N/SR/TB"]
                    if k > 0:
                        filters.append(f"afade=t=in:d={JOIN_FADE_SECONDS}")
                    if length and k < len(parts) - 1:
                        filters.append(f"afade=t=out:st={max(0.0, length - JOIN_FADE_SECONDS):.6f}:d={JOIN_FADE_SECONDS}")
                    if length:
                        filters.append(f"apad=whole_dur={length:.6f}")
                    wav = part + ".wav"
                    temp.append(wav)
                    code = _run([ffmpeg, "-hide_banner", "-loglevel", "warning", "-y", "-i", part, "-vn",
                                 "-af", ",".join(filters), "-c:a", "pcm_s16le", wav], log)
                    if code != 0:
                        raise RuntimeError(f"音声の切り出しに失敗しました (code {code})")
                    af.write(_list_line(wav))
            code = _run([
                ffmpeg, "-hide_banner", "-loglevel", "warning", "-y",
                "-f", "concat", "-safe", "0", "-i", video_list,
                "-f", "concat", "-safe", "0", "-i", audio_list,
                "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart", output,
            ], log)
    finally:
        for path in temp:
            try:
                os.remove(path)
            except OSError:
                pass
    if code != 0:
        raise RuntimeError(f"動画の結合に失敗しました (code {code})")


def decode_gray(ffmpeg, path, start, count=None, size=SIG_SIZE):
    w, h = size
    cmd = [ffmpeg, "-v", "error", "-ss", f"{max(0.0, start):.6f}", "-i", path, "-an", "-fps_mode", "passthrough"]
    if count:
        cmd += ["-frames:v", str(count)]
    cmd += ["-vf", f"scale={w}:{h}:flags=area,format=gray", "-f", "rawvideo", "-"]
    data = subprocess.run(cmd, capture_output=True, **osp.popen_kwargs()).stdout
    n = w * h
    return [data[i:i + n] for i in range(0, len(data) - n + 1, n)]


def _sad(a, b):
    return sum(abs(x - y) for x, y in zip(a, b))


def find_join(ffmpeg, part_a, duration_a, part_b, at_b, fps, nominal_a, window=1.2):
    # Seek half a frame before the wanted frame: -ss keeps frames at or after the seek point, so
    # seeking exactly onto a frame time (or rounding the start) can be off by one frame.
    head = decode_gray(ffmpeg, part_b, (round(at_b * fps) - 0.5) / fps, 1)
    if not head:
        return None
    first = max(0, math.ceil(max(0.0, duration_a - window) * fps - 1e-6))
    tail = decode_gray(ffmpeg, part_a, (first - 0.5) / fps, None)
    if not tail:
        return None
    scores = [_sad(head[0], frame) for frame in tail]
    best = min(scores)
    # Frames as good as the best match (static content) are candidates; the tolerance is relative to the
    # best match, not to the spread, so a flash or scene cut in the window cannot pull in a neighbour.
    near = [i for i, s in enumerate(scores) if s <= best + max(best * 0.5, len(head[0]) * 0.5)]
    pick = min(near, key=lambda i: abs((first + i) / fps - nominal_a))
    return (first + pick) / fps


def video_encode_args(encoder, fps, no_bframes=False, key_frames=None):
    """Output pixel format, encoder options, GOP and key frames: the video-encoding part shared by
    recordings (build_command) and the capacity measurement (capacity.py)."""
    # VideoToolbox takes NV12 directly, which avoids a CPU conversion; the other encoders get yuv420p.
    out_pix_fmt = "nv12" if encoder == "h264_videotoolbox" else "yuv420p"
    return [
        "-pix_fmt", out_pix_fmt, *ENCODERS[encoder], "-g", str(fps * 2), *(["-bf", "0"] if no_bframes else []),
        *(["-force_key_frames", ",".join(f"{t:.6f}" for t in key_frames), *FORCED_IDR.get(encoder, [])] if key_frames else []),
    ]


def build_command(ffmpeg, width, height, fps, audio_pipe, sample_rate, channels, out_w, out_h, encoder, output, trim_start=0.0,
                  no_bframes=False, key_frames=None, pix_fmt="bgra"):
    video_filter = []
    if (width, height) != (out_w, out_h):
        video_filter = [
            "-vf",
            f"scale={out_w}:{out_h}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2",
        ]
    # ScreenCaptureKit NV12 is BT.709 video range (§5.4); tell FFmpeg so the encoder tags and converts correctly.
    color_tags = ["-color_range", "tv", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"] if pix_fmt == "nv12" else []
    return [
        ffmpeg, "-hide_banner", "-loglevel", "warning", "-y",
        "-thread_queue_size", "16", "-probesize", "32", "-analyzeduration", "0",
        *color_tags,
        "-f", "rawvideo", "-pix_fmt", pix_fmt, "-video_size", f"{width}x{height}", "-framerate", str(fps),
        "-i", "pipe:0",
        "-thread_queue_size", "1024", "-probesize", "32", "-analyzeduration", "0",
        "-f", "s16le", "-ar", str(sample_rate), "-ch_layout", "stereo" if channels == 2 else "mono",
        "-i", audio_pipe,
        "-map", "0:v:0", "-map", "1:a:0",
        *(["-ss", f"{trim_start:.3f}"] if trim_start > 0 else []),
        *video_filter,
        *video_encode_args(encoder, fps, no_bframes, key_frames),
        "-c:a", "aac", "-b:a", "192k",
        *(["-movflags", "+frag_keyframe+empty_moov+default_base_moof"] if output.lower().endswith(".mp4") else []),
        output,
    ]
