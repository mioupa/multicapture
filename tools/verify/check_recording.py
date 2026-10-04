#!/usr/bin/env python3
"""Verify a recording of the barcode/flash/beep test video (stdlib + ffmpeg/ffprobe only).

  python3 check_recording.py output.mp4 [--fps 30] [--source-duration S]

Video: every frame's barcode is decoded (ffmpeg reduces each frame to a 24x3 gray image: the 24
cells of the top band, of the bottom band, and a luma strip) -> frames read, first/last number,
missing numbers, duplicates, backwards jumps, with output time positions.
A/V: flash onsets (frame luma rising edge, first bright frame pts) vs beep onsets (1 kHz burst in the
decoded 48 kHz audio) -> offset = beep - flash (positive = audio late).
PASS (requirements Phase 2): no missing/duplicate/backwards frames and |offset| <= 40 ms.
Writes <output>.check.json; exit code 0 = PASS, 1 = FAIL.
"""
import argparse
import array
import json
import os
import statistics
import subprocess
import sys

CELLS = 24
OFFSET_LIMIT = 0.040


def run_probe(ffprobe, path, args):
    r = subprocess.run([ffprobe, "-v", "error", *args, path], capture_output=True, text=True)
    return r.stdout


def probe_streams(path, ffprobe="ffprobe"):
    out = json.loads(run_probe(ffprobe, path, ["-show_streams", "-show_format", "-of", "json"]) or "{}")
    v = next((s for s in out.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in out.get("streams", []) if s.get("codec_type") == "audio"), None)
    return v, a, out.get("format", {})


def video_pts(path, ffprobe="ffprobe"):
    txt = run_probe(ffprobe, path, ["-select_streams", "v:0", "-show_entries", "packet=pts_time", "-of", "csv=p=0"])
    vals = []
    for line in txt.splitlines():
        try:
            vals.append(float(line.strip().rstrip(",")))
        except ValueError:
            vals.append(None)
    if any(v is None for v in vals):
        return None
    return sorted(vals)


def decode_frames(path, ffmpeg="ffmpeg", progress=False):
    """-> list of rows (bytes, 72): row0 top-band cells, row1 bottom-band cells, row2 luma strip."""
    vf = ("format=gray,split=3[a][b][c];"
          "[a]crop=iw:ih*0.05:0:ih*0.02,scale=24:1:flags=area[t];"
          "[b]crop=iw:ih*0.05:0:ih*0.93,scale=24:1:flags=area[u];"
          "[c]crop=iw:ih*0.6:0:ih*0.2,scale=24:1:flags=area[l];"
          "[t][u][l]vstack=inputs=3")
    cmd = [ffmpeg, "-v", "error", "-i", path, "-an", "-filter_complex", vf,
           "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    frames, buf = [], b""
    size = CELLS * 3
    while True:
        chunk = p.stdout.read(size * 2048)
        if not chunk:
            break
        buf += chunk
        n = len(buf) // size
        for i in range(n):
            frames.append(buf[i * size:(i + 1) * size])
        buf = buf[n * size:]
        if progress and len(frames) % 20480 < 2048:
            print(f"\r  decoded {len(frames)} frames", end="", file=sys.stderr, flush=True)
    p.wait()
    if progress:
        print(file=sys.stderr)
    return frames


def decode_cells(cells):
    """24 cell means -> frame number or None (checks sentinels, parity cell and black cell)."""
    white = (cells[0] + cells[23]) / 2
    black = cells[22]
    if white - black < 80 or abs(cells[0] - cells[23]) > 60:
        return None
    thr = (white + black) / 2
    bits = [1 if c > thr else 0 for c in cells]
    if bits[0] != 1 or bits[23] != 1 or bits[22] != 0:
        return None
    if sum(bits[1:21]) % 2 != bits[21]:
        return None
    return sum(b << k for k, b in enumerate(bits[1:21]))


def decode_all(frames):
    nums, luma, conflicts = [], [], 0
    for f in frames:
        a, b = decode_cells(f[0:24]), decode_cells(f[24:48])
        if a is not None and b is not None and a != b:
            conflicts += 1
            a = None if abs(a - b) > 0 else a
            b = None
        nums.append(a if a is not None else b)
        luma.append(sum(f[48:72]) / 24)
    return nums, luma, conflicts


def ranges(vals):
    """sorted ints -> 'a-b, c, ...' (compact)."""
    out, vals = [], sorted(vals)
    i = 0
    while i < len(vals):
        j = i
        while j + 1 < len(vals) and vals[j + 1] == vals[j] + 1:
            j += 1
        out.append(str(vals[i]) if i == j else f"{vals[i]}-{vals[j]}")
        i = j + 1
    return ", ".join(out)


def analyze_video(nums, times):
    """Sequence analysis. times[i] = presentation time of output frame i."""
    res = {"undecoded": [], "missing_events": [], "dup_events": [], "backward_events": []}
    prev, prev_i = None, None
    seen = {}
    for i, n in enumerate(nums):
        if n is None:
            res["undecoded"].append(i)
            continue
        seen[n] = seen.get(n, 0) + 1
        if prev is not None:
            d = n - prev
            if d == 0:
                res["dup_events"].append({"frame": n, "index": i, "time": times[i]})
            elif d > 1:
                res["missing_events"].append({"from": prev + 1, "to": n - 1, "count": d - 1, "index": i, "time": times[i]})
            elif d < 0:
                res["backward_events"].append({"from": prev, "to": n, "index": i, "time": times[i]})
        prev, prev_i = n, i
    ok = [n for n in nums if n is not None]
    res["first"], res["last"] = (ok[0], ok[-1]) if ok else (None, None)
    res["frames_read"] = len(nums)
    res["distinct"] = len(seen)
    res["missing_count"] = sum(e["count"] for e in res["missing_events"])
    # The first and last frames are held while the recording starts and after the source ends;
    # report those holds separately instead of as duplicates.
    idx = [i for i, n in enumerate(nums) if n is not None]
    head = tail = 0
    while head + 1 < len(idx) and nums[idx[head + 1]] == nums[idx[0]]:
        head += 1
    while tail + 1 < len(idx) and nums[idx[-2 - tail]] == nums[idx[-1]]:
        tail += 1
    if ok and ok[0] == ok[-1]:
        tail = 0
    edge = set(idx[1:head + 1]) | set(idx[len(idx) - tail:]) if tail else set(idx[1:head + 1])
    res["dup_events"] = [e for e in res["dup_events"] if e["index"] not in edge]
    res["head_hold"], res["tail_hold"] = head, tail
    res["dup_count"] = sum(c - 1 for c in seen.values() if c > 1) - head - tail
    # numbers absent from the min..max range of what was seen (also covers re-runs after backward jumps)
    res["missing_numbers"] = [x for x in range(min(seen), max(seen) + 1) if x not in seen] if seen else []
    return res


def beep_onsets(path, ffmpeg="ffmpeg", a_start=0.0, bin_samples=48, rate=48000, min_gap=0.5):
    """Decode audio to 48 kHz mono s16 and find 1 kHz burst onsets (rising through 50% of the peak
    level, 1 ms resolution). -> (times, peak)"""
    p = subprocess.Popen([ffmpeg, "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", str(rate),
                          "-f", "s16le", "-"], stdout=subprocess.PIPE)
    peaks = array.array("h")
    carry = b""
    while True:
        chunk = p.stdout.read(1 << 22)
        if not chunk:
            break
        chunk = carry + chunk
        keep = len(chunk) - len(chunk) % (bin_samples * 2)
        carry = chunk[keep:]
        a = array.array("h")
        a.frombytes(chunk[:keep])
        for i in range(0, len(a), bin_samples):
            seg = a[i:i + bin_samples]
            m, n = max(seg), -min(seg)
            peaks.append(max(m, n, 0) if max(m, n) < 32767 else 32767)
    p.wait()
    if not peaks:
        return [], 0
    peak = max(peaks)
    if peak < 300:
        return [], peak
    thr = peak * 0.5
    out, above, last = [], False, -1e9
    dt = bin_samples / rate
    for i, v in enumerate(peaks):
        if v >= thr:
            t = a_start + i * dt
            if not above and t - last >= min_gap:
                out.append(t)
                last = t
            above = True
        else:
            above = False
    return out, peak


def flash_onsets(luma, times):
    if len(luma) < 3:
        return []
    s = sorted(luma)
    low, high = s[int(len(s) * 0.30)], s[min(len(s) - 1, int(len(s) * 0.995))]
    if high - low < 40:
        return []
    mid = (low + high) / 2
    out = []
    for i in range(1, len(luma)):
        if luma[i] > mid >= luma[i - 1]:
            out.append(times[i])
    return out


def pair(flashes, beeps, window=0.5):
    import bisect
    beeps = sorted(beeps)
    used, out, unpaired_f = set(), [], []
    for t in flashes:
        i = bisect.bisect_left(beeps, t)
        c = [j for j in (i - 1, i) if 0 <= j < len(beeps)]
        if not c:
            unpaired_f.append(t)
            continue
        j = min(c, key=lambda j: abs(beeps[j] - t))
        if abs(beeps[j] - t) <= window and j not in used:
            used.add(j)
            out.append((t, beeps[j] - t))
        else:
            unpaired_f.append(t)
    unpaired_b = [b for j, b in enumerate(beeps) if j not in used]
    return out, unpaired_f, unpaired_b


def group_outliers(pairs, limit=OFFSET_LIMIT, gap=1.5):
    """pairs with |offset| > limit -> list of groups {from, to, n, median_ms}"""
    bad = [(t, o) for t, o in pairs if abs(o) > limit]
    groups, cur = [], []
    for t, o in bad:
        if cur and t - cur[-1][0] > gap:
            groups.append(cur)
            cur = []
        cur.append((t, o))
    if cur:
        groups.append(cur)
    return [{"from": g[0][0], "to": g[-1][0], "n": len(g), "median_ms": statistics.median(o for _, o in g) * 1000} for g in groups]


def check(path, fps=30, source_duration=None, ffmpeg="ffmpeg", ffprobe="ffprobe", progress=False):
    v, a, fmt = probe_streams(path, ffprobe)
    if v is None:
        raise SystemExit("no video stream")
    frames = decode_frames(path, ffmpeg, progress)
    pts = video_pts(path, ffprobe)
    vstart = float(v.get("start_time") or 0)
    pts_ok = pts is not None and len(pts) == len(frames)
    times = pts if pts_ok else [vstart + i / fps for i in range(len(frames))]
    nums, luma, conflicts = decode_all(frames)
    vid = analyze_video(nums, times)
    r = {"file": os.path.abspath(path), "fps": fps, "size": f"{v.get('width')}x{v.get('height')}",
         "duration": float(fmt.get("duration") or 0), "pts_from_container": pts_ok, "band_conflicts": conflicts, "video": vid}
    flashes = flash_onsets(luma, times)
    if a is not None:
        beeps, peak = beep_onsets(path, ffmpeg, float(a.get("start_time") or 0))
    else:
        beeps, peak = [], 0
    pairs, uf, ub = pair(flashes, beeps)
    offs = [o for _, o in pairs]
    av = {"audio": a is not None, "flashes": len(flashes), "beeps": len(beeps), "paired": len(pairs),
          "unpaired_flashes": uf, "unpaired_beeps": ub, "audio_peak": peak}
    if offs:
        av.update({"median_ms": statistics.median(offs) * 1000, "mean_ms": statistics.fmean(offs) * 1000,
                   "p2p_ms": (max(offs) - min(offs)) * 1000, "worst_ms": max(offs, key=abs) * 1000})
    av["outliers"] = group_outliers(pairs)
    av["outlier_count"] = sum(g["n"] for g in av["outliers"])
    r["av"] = av
    cov = {}
    if source_duration and vid["first"] is not None:
        total = round(source_duration * fps)
        cov = {"expected_frames": total, "head_missing": vid["first"], "tail_missing": total - 1 - vid["last"]}
    r["coverage"] = cov
    fails = []
    if vid["missing_count"]:
        fails.append("missing frames")
    if vid["dup_count"]:
        fails.append("duplicate frames")
    if vid["backward_events"]:
        fails.append("backwards jumps")
    if vid["undecoded"]:
        fails.append("undecodable frames")
    if cov and (cov["head_missing"] > 2 or cov["tail_missing"] > 2):
        fails.append("coverage")
    if not av["audio"]:
        fails.append("no audio")
    elif not offs:
        fails.append("no A/V pairs")
    else:
        if av["outlier_count"]:
            fails.append("A/V offset > 40 ms")
        if len(uf) > 1 or len(ub) > 1:
            fails.append("unpaired flash/beep")
    r["fails"], r["pass"] = fails, not fails
    return r


def fmt_t(t):
    return f"{t:.2f}s"


def summarize(r):
    v, av = r["video"], r["av"]
    L = [f"{os.path.basename(r['file'])}  {r['size']}  {r['duration']:.1f}s  fps={r['fps']}  pts={'container' if r['pts_from_container'] else 'assumed CFR'}"]
    L.append(f"frames read {v['frames_read']}  distinct {v['distinct']}  first {v['first']}  last {v['last']}  undecoded {len(v['undecoded'])}"
             + (f" ({ranges(v['undecoded'][:50])}...)" if v["undecoded"] else ""))
    L.append(f"missing {v['missing_count']}  duplicates {v['dup_count']}  backwards {len(v['backward_events'])}"
             f"  (held: first frame +{v.get('head_hold', 0)}, last frame +{v.get('tail_hold', 0)})")
    for e in v["missing_events"][:6]:
        L.append(f"  missing {e['from']}-{e['to']} ({e['count']}) at out frame {e['index']} t={fmt_t(e['time'])}")
    if len(v["missing_events"]) > 6:
        L.append(f"  ... {len(v['missing_events']) - 6} more missing events")
    if v["dup_events"]:
        L.append("  dup frames: " + ranges([e["frame"] for e in v["dup_events"]]) + " at t=" + ", ".join(fmt_t(e["time"]) for e in v["dup_events"][:5]))
    for e in v["backward_events"][:4]:
        L.append(f"  backwards {e['from']}->{e['to']} at out frame {e['index']} t={fmt_t(e['time'])}")
    if r["coverage"]:
        c = r["coverage"]
        L.append(f"coverage: expected {c['expected_frames']} frames; head missing {c['head_missing']}, tail missing {c['tail_missing']}")
    if not av["audio"]:
        L.append("A/V: no audio stream")
    elif "median_ms" not in av:
        L.append(f"A/V: no pairs (flashes {av['flashes']}, beeps {av['beeps']}, audio peak {av['audio_peak']})")
    else:
        L.append(f"A/V: flashes {av['flashes']} beeps {av['beeps']} paired {av['paired']} (unpaired f/b {len(av['unpaired_flashes'])}/{len(av['unpaired_beeps'])})")
        L.append(f"  offset ms (beep-flash): median {av['median_ms']:+.1f}  mean {av['mean_ms']:+.1f}  p2p {av['p2p_ms']:.1f}  worst {av['worst_ms']:+.1f}")
        for g in av["outliers"][:6]:
            L.append(f"  outliers >40ms: {g['n']} at {fmt_t(g['from'])}-{fmt_t(g['to'])} median {g['median_ms']:+.1f} ms")
        if len(av["outliers"]) > 6:
            L.append(f"  ... {len(av['outliers']) - 6} more outlier groups")
    L.append("RESULT: " + ("PASS" if r["pass"] else "FAIL (" + "; ".join(r["fails"]) + ")"))
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input")
    ap.add_argument("--fps", type=float, default=30)
    ap.add_argument("--source-duration", type=float, default=None, help="duration of the source video (s): also checks head/tail coverage")
    ap.add_argument("--ffmpeg", default="ffmpeg")
    ap.add_argument("--ffprobe", default="ffprobe")
    ap.add_argument("--json", default=None, help="default <input>.check.json")
    a = ap.parse_args()
    r = check(a.input, a.fps, a.source_duration, a.ffmpeg, a.ffprobe, progress=sys.stderr.isatty())
    jp = a.json or a.input + ".check.json"
    with open(jp, "w") as f:
        json.dump(r, f, indent=1)
    print(summarize(r))
    print(f"json: {jp}")
    sys.exit(0 if r["pass"] else 1)


if __name__ == "__main__":
    main()
