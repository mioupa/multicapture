import os
import shutil
import subprocess

from .config import app_dir

CREATE_NO_WINDOW = 0x08000000

ENCODERS = {
    "h264_nvenc": ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "23", "-b:v", "0"],
    "h264_amf": ["-c:v", "h264_amf", "-quality", "balanced", "-rc", "cqp", "-qp_i", "21", "-qp_p", "23"],
    "h264_qsv": ["-c:v", "h264_qsv", "-preset", "medium", "-global_quality", "23"],
    "libx264": ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"],
}

ENCODER_LABELS = {
    "h264_nvenc": "NVIDIA NVENC",
    "h264_amf": "AMD AMF",
    "h264_qsv": "Intel Quick Sync",
    "libx264": "CPU (x264)",
}

_detected = None


def find_ffmpeg():
    base = app_dir()
    for candidate in (
        os.path.join(base, "ffmpeg", "ffmpeg.exe"),
        os.path.join(base, "ffmpeg.exe"),
        os.path.join(base, "_internal", "ffmpeg.exe"),
    ):
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
        return subprocess.run(cmd, capture_output=True, timeout=20, creationflags=CREATE_NO_WINDOW).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def detect_encoder(ffmpeg, preference="auto"):
    global _detected
    if preference in ENCODERS and preference != "auto":
        return preference
    if _detected is None:
        _detected = next((e for e in ("h264_nvenc", "h264_amf", "h264_qsv") if _works(ffmpeg, e)), "libx264")
    return _detected


def concat(ffmpeg, parts, output, log_path):
    list_path = output + ".parts.txt"
    with open(list_path, "w", encoding="utf-8") as f:
        for part, duration in parts:
            escaped = part.replace("\\", "/").replace("'", "'\\''")
            f.write(f"file '{escaped}'\n")
            if duration:
                f.write(f"outpoint {duration:.6f}\n")
    inputs = []
    chains = []
    for k, (part, duration) in enumerate(parts, start=1):
        inputs += ["-i", part]
        trim = f"atrim=end={duration:.6f}," if duration else ""
        chains.append(f"[{k}:a]{trim}asetpts=N/SR/TB[a{k}]")
    labels = "".join(f"[a{k}]" for k in range(1, len(parts) + 1))
    graph = ";".join(chains) + f";{labels}concat=n={len(parts)}:v=0:a=1[aout]"
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "warning", "-y",
        "-f", "concat", "-safe", "0", "-i", list_path,
        *inputs,
        "-filter_complex", graph,
        "-map", "0:v:0", "-map", "[aout]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", output,
    ]
    try:
        with open(log_path, "w", encoding="utf-8", errors="replace") as log:
            code = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=log, creationflags=CREATE_NO_WINDOW).returncode
    finally:
        try:
            os.remove(list_path)
        except OSError:
            pass
    if code != 0:
        raise RuntimeError(f"動画の結合に失敗しました (code {code})")


def build_command(ffmpeg, width, height, fps, audio_pipe, sample_rate, channels, out_w, out_h, encoder, output, trim_start=0.0,
                  no_bframes=False):
    video_filter = []
    if (width, height) != (out_w, out_h):
        video_filter = [
            "-vf",
            f"scale={out_w}:{out_h}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2",
        ]
    return [
        ffmpeg, "-hide_banner", "-loglevel", "warning", "-y",
        "-thread_queue_size", "64", "-probesize", "32", "-analyzeduration", "0",
        "-f", "rawvideo", "-pix_fmt", "bgra", "-video_size", f"{width}x{height}", "-framerate", str(fps),
        "-i", "pipe:0",
        "-thread_queue_size", "1024", "-probesize", "32", "-analyzeduration", "0",
        "-f", "s16le", "-ar", str(sample_rate), "-ch_layout", "stereo" if channels == 2 else "mono",
        "-i", audio_pipe,
        "-map", "0:v:0", "-map", "1:a:0",
        *(["-ss", f"{trim_start:.3f}"] if trim_start > 0 else []),
        *video_filter,
        "-pix_fmt", "yuv420p", *ENCODERS[encoder], "-g", str(fps * 2), *(["-bf", "0"] if no_bframes else []),
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
        output,
    ]
