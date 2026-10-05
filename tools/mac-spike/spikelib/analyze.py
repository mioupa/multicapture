"""Analysis helpers: WAV, Goertzel, onsets, A/V offsets, barcode decoding (stdlib only)."""
import array
import bisect
import csv
import math
import statistics
import struct
import wave


# ---------------------------------------------------------------- audio
def read_wav(path):
    """-> (rate, channels, mono samples as array('f') in [-1, 1]). Channels are averaged."""
    with wave.open(path, "rb") as w:
        ch, width, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    if width == 2:
        a = array.array("h")
        a.frombytes(raw)
        scale = 1 / 32768.0
    elif width == 4:
        a = array.array("i")
        a.frombytes(raw)
        scale = 1 / 2147483648.0
    elif width == 1:
        a = array.array("B", raw)
        a = array.array("h", [(x - 128) << 8 for x in a])
        scale = 1 / 32768.0
    else:
        raise ValueError(f"unsupported sample width {width}")
    if ch == 1:
        mono = array.array("f", (x * scale for x in a))
    else:
        mono = array.array("f", (sum(a[i:i + ch]) * scale / ch for i in range(0, len(a) - ch + 1, ch)))
    return rate, ch, mono


def goertzel_db(samples, rate, freq):
    """Amplitude of `freq` in dBFS (a full-scale sine gives 0 dB)."""
    n = len(samples)
    if n == 0:
        return -200.0
    k = 2 * math.cos(2 * math.pi * freq / rate)
    s1 = s2 = 0.0
    for x in samples:
        s0 = x + k * s1 - s2
        s2 = s1
        s1 = s0
    power = s1 * s1 + s2 * s2 - k * s1 * s2
    amp = 2 * math.sqrt(max(power, 0.0)) / n
    return 20 * math.log10(amp) if amp > 1e-10 else -200.0


def envelope(samples, rate, bin_ms=1.0):
    """Peak |x| per bin -> (times_s relative to sample 0, values)."""
    b = max(1, int(round(rate * bin_ms / 1000.0)))
    times, vals = [], []
    for i in range(0, len(samples) - b + 1, b):
        seg = samples[i:i + b]
        vals.append(max(max(seg), -min(seg)))
        times.append(i / rate)
    return times, vals


def onsets(times, values, threshold, min_gap):
    """Times where `values` rises through `threshold`, at least min_gap apart."""
    out, above, last = [], False, -1e18
    for t, v in zip(times, values):
        if v >= threshold:
            if not above and t - last >= min_gap:
                out.append(t)
                last = t
            above = True
        else:
            above = False
    return out


def read_audio_log(path):
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            rows.append({k: float(v) for k, v in r.items() if v not in (None, "")})
    return rows


def beep_onsets(wav_path, audio_log_csv, thresh_frac=0.5, min_gap=0.5):
    """Beep onset host times (seconds). Sample index -> host time via per-callback host_time +
    (index - first sample of callback) / rate. Callbacks are consecutive; `frames` gives counts."""
    rate, ch, x = read_wav(wav_path)
    log = read_audio_log(audio_log_csv)
    starts, cum = [], 0
    for r in log:
        starts.append(cum)
        cum += int(r["frames"])
    peak = max(max(x), -min(x)) if len(x) else 0
    if peak < 1e-4:
        return [], {"rate": rate, "peak": peak, "samples": len(x), "logged_samples": cum}
    thr = peak * thresh_frac
    times, vals = envelope(x, rate, 1.0)
    onset_bins = onsets(times, vals, thr, min_gap)
    out = []
    for tb in onset_bins:
        i = int(round(tb * rate))
        end = min(len(x), i + int(rate * 0.002) + 1)
        while i < end and abs(x[i]) < thr:
            i += 1
        k = bisect.bisect_right(starts, i) - 1
        if k < 0 or k >= len(log):
            continue
        out.append(log[k]["host_time"] + (i - starts[k]) / rate)
    return out, {"rate": rate, "peak": peak, "samples": len(x), "logged_samples": cum}


# ---------------------------------------------------------------- video
def read_frame_log(path):
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            row = {}
            for k, v in r.items():
                try:
                    row[k] = float(v)
                except (TypeError, ValueError):
                    row[k] = v
            rows.append(row)
    return rows


def _pct(sorted_vals, p):
    return sorted_vals[min(len(sorted_vals) - 1, int(len(sorted_vals) * p))]


def flash_onsets(frame_log_csv, col="pts"):
    """Rising luma edges across the midpoint of robust low/high levels.
    -> list of (onset_time = time of first bright frame, previous_dark_time)."""
    rows = read_frame_log(frame_log_csv) if isinstance(frame_log_csv, str) else frame_log_csv
    if len(rows) < 3:
        return []
    lum = sorted(r["luma"] for r in rows)
    low, high = _pct(lum, 0.30), _pct(lum, 0.985)
    if high - low < 20:
        return []
    mid = (low + high) / 2
    out, prev = [], None
    for r in rows:
        bright = r["luma"] > mid
        if prev is not None and not prev["luma"] > mid and bright:
            out.append((r[col], prev[col]))
        prev = r
    return out


def pair_offsets(flashes, beeps, window=0.5):
    """flashes: list of times (or (t, prev) tuples); beeps: list of times.
    -> list of (flash_time, beep - flash) using the nearest beep within +-window."""
    ft = [f[0] if isinstance(f, (tuple, list)) else f for f in flashes]
    beeps = sorted(beeps)
    out = []
    for t in ft:
        i = bisect.bisect_left(beeps, t)
        cands = [beeps[j] for j in (i - 1, i) if 0 <= j < len(beeps)]
        if not cands:
            continue
        b = min(cands, key=lambda v: abs(v - t))
        if abs(b - t) <= window:
            out.append((t, b - t))
    return out


def stats(values, times=None):
    """Basic stats of a list. If `times` is given (seconds), also slope per minute (least squares)."""
    n = len(values)
    if n == 0:
        return {"n": 0}
    s = {"n": n, "mean": statistics.fmean(values), "median": statistics.median(values),
         "stdev": statistics.pstdev(values) if n > 1 else 0.0, "min": min(values), "max": max(values),
         "p2p": max(values) - min(values)}
    if times is not None and n >= 3:
        mt = statistics.fmean(times)
        mv = s["mean"]
        den = sum((t - mt) ** 2 for t in times)
        s["slope_per_min"] = sum((t - mt) * (v - mv) for t, v in zip(times, values)) / den * 60 if den else 0.0
    return s


# ---------------------------------------------------------------- barcode
CELLS = 24


def decode_barcode_detail(gray, width, height, thresh=128):
    """Decode the make_media.py barcode from 8-bit gray bytes.
    -> dict(frame, ok, sentinels, parity_ok, bits). frame is None if sentinels/parity are wrong."""
    band = round(96 * height / 1080)
    cw = width / CELLS
    ys = [int(band * f) for f in (0.35, 0.5, 0.65)]
    cells = []
    for c in range(CELLS):
        x0, x1 = int(c * cw + cw * 0.3), max(int(c * cw + cw * 0.3) + 1, int(c * cw + cw * 0.7))
        tot = n = 0
        for y in ys:
            row = gray[y * width + x0:y * width + x1]
            tot += sum(row)
            n += len(row)
        cells.append(tot / n if n else 0)
    bits = [1 if v > thresh else 0 for v in cells]
    sentinels = bits[0] == 1 and bits[23] == 1
    frame = sum(b << k for k, b in enumerate(bits[1:21]))
    parity_ok = (sum(bits[1:21]) % 2) == bits[21]
    ok = sentinels and parity_ok and bits[22] == 0
    return {"frame": frame if ok else None, "ok": ok, "sentinels": sentinels, "parity_ok": parity_ok,
            "bits": bits, "cell_means": cells}


def decode_barcode(gray_bytes, width, height):
    return decode_barcode_detail(gray_bytes, width, height)["frame"]
