#!/usr/bin/env python3
"""S6: crisp 1920x1080 capture, crop correctness, oversize windows.

Variants: --dsf1 on|off (with / without --force-device-scale-factor=1; comma list allowed) x
--capture-resolution automatic,best,nominal. For each: one Chrome, content 1920x1080, plays
test_5min.mp4 muted; the helper captures a few seconds with --dump-dir (seq 30 and 60) and a
frame-log. Each dumped PNG is decoded (barcode -> frame number), checked for crop correctness
(top-left / top-right corner blocks must be the white barcode sentinels, i.e. no title-bar rows
or borders), blur (count of intermediate pixels along barcode edges) and PSNR against the same
frame extracted from the source mp4 (ffmpeg psnr filter on gray).
--oversize additionally requests content larger than the display (default 3840x2560 and
4096x2700) and reports the inner size Chrome actually achieved and what the capture delivers.

PASS (default variant dsf1=on / automatic): output 1920x1080, barcode decodes, crop OK, PSNR >= 35 dB
(threshold is a first guess; the numbers are what matters). Other variants are reported only."""
import argparse
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from spikelib import runner, analyze, chrome as chromelib, MEDIA_DIR  # noqa: E402


def ffmpeg(args, **kw):
    return subprocess.run(["ffmpeg", "-v", "error", "-y", *map(str, args)], capture_output=True, **kw)


def png_to_gray(path, w=None, h=None):
    r = ffmpeg(["-i", path, "-pix_fmt", "gray", "-f", "rawvideo", "-"])
    return r.stdout


def png_size(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                        "-of", "csv=p=0", path], capture_output=True, text=True)
    try:
        w, h = r.stdout.strip().split(",")
        return int(w), int(h)
    except ValueError:
        return None


def crop_gray(raw, w, h, x, y, cw, chh):
    return b"".join(raw[(y + r) * w + x:(y + r) * w + x + cw] for r in range(chh))


def block_min(raw, w, x, y, bw=6, bh=6):
    return min(min(raw[(y + r) * w + x:(y + r) * w + x + bw]) for r in range(bh))


def blur_px(raw, w, band):
    """mean number of intermediate-valued pixels along the barcode edges (row at mid band)."""
    row = raw[(band // 2) * w:(band // 2) * w + w]
    cw = w // 24
    tot = 0
    for c in range(1, 24):
        seg = row[c * cw - 6:c * cw + 6]
        tot += sum(1 for v in seg if 60 < v < 200)
    return tot / 23


def psnr_vs_source(png, src_mp4, frame_no, tmp, w, h):
    ref = os.path.join(tmp, f"ref{frame_no}.png")
    ffmpeg(["-i", src_mp4, "-vf", f"select=eq(n\\,{frame_no})", "-frames:v", 1, ref])
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", png, "-i", ref, "-lavfi",
                        f"[0:v]format=gray,scale={w}:{h}:flags=bicubic[a];[1:v]format=gray,scale={w}:{h}:flags=bicubic[b];[a][b]psnr",
                        "-f", "null", "-"], capture_output=True, text=True)
    m = re.search(r"average:([\d.]+|inf)", r.stderr)
    return (float(m.group(1)) if m else None)


def analyze_dump(png, src, tmp, cw, ch, expect=(1920, 1080), video_rect=None):
    out = {"png": os.path.basename(png)}
    sz = png_size(png)
    out["png_size"] = sz
    if not sz:
        return out
    w, h = sz
    raw = png_to_gray(png)
    if len(raw) != w * h:
        out["error"] = f"raw size mismatch {len(raw)} != {w * h}"
        return out
    vx, vy, vw, vh = video_rect or (0, 0, w, h)
    sub = crop_gray(raw, w, h, vx, vy, vw, vh) if video_rect else raw
    d = analyze.decode_barcode_detail(sub, vw, vh)
    out["frame"] = d["frame"]
    out["barcode_ok"] = d["ok"]
    band = round(96 * vh / 1080)
    out["corner_tl_white"] = block_min(sub, vw, 0, 0) > 200
    out["corner_tr_white"] = block_min(sub, vw, vw - 6, 0) > 200
    out["crop_ok"] = bool(out["corner_tl_white"] and out["corner_tr_white"] and d["ok"])
    out["blur_px"] = blur_px(sub, vw, band)
    if d["frame"] is not None and not video_rect:
        out["psnr_db"] = psnr_vs_source(png, src, d["frame"], tmp, w, h)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dsf1", default="on,off", help="on, off or on,off")
    ap.add_argument("--capture-resolution", default="automatic,best,nominal")
    ap.add_argument("--seconds", type=float, default=4)
    ap.add_argument("--size", default="1920x1080")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--oversize", action="store_true")
    ap.add_argument("--oversize-sizes", default="3840x2560,4096x2700")
    ap.add_argument("--dump-seqs", default="30,60")
    ap.add_argument("--skip-perm-check", action="store_true")
    a = ap.parse_args()
    cw, ch = runner.parse_size(a.size)
    run = runner.Run("S6", None, a)
    run.preflight(need_screen=True, skip=a.skip_perm_check)
    src = os.path.join(MEDIA_DIR, "test_5min.mp4")
    results, err = [], None
    scale_seen = set()

    def one_capture(inst, tag, size, top, capres, out_w, out_h, video_rect_fn=None):
        dump = os.path.join(run.dir, f"dump-{tag}")
        os.makedirs(dump, exist_ok=True)
        wid = chromelib.find_window_id(inst.pid, "mc-s6")
        log = os.path.join(run.dir, f"frames-{tag}.csv")
        args = ["capture", "--window-id", wid, "--size", f"{out_w}x{out_h}", "--fps", a.fps,
                "--crop", f"0,{top},{out_w},{out_h}", "--capture-resolution", capres, "--frame-log", log,
                "--dump-dir", dump, "--dump-seqs", a.dump_seqs, "--duration", a.seconds]
        h = run.start_helper(args, tag)
        ff = h.wait_for("first_frame", 15)
        h.wait_for("stopped", a.seconds + 15)
        run.stop_helpers([h])
        run.helpers.remove(h)
        rec = {"tag": tag, "capture_resolution": capres, "window_id": wid, "first_frame": ff,
               "errors": h.find("error"), "dumps": []}
        if ff:
            scale_seen.add(ff.get("scale_factor"))
        for f in sorted(os.listdir(dump)):
            if f.endswith(".png"):
                vr = video_rect_fn(ff) if video_rect_fn else None
                rec["dumps"].append(analyze_dump(os.path.join(dump, f), src, dump, out_w, out_h, video_rect=vr))
        return rec

    try:
        for dsf in [x.strip() for x in a.dsf1.split(",")]:
            inst = run.launch_chrome(f"dsf{dsf}", run.player_url(src="/test_5min.mp4", muted=1, loop=1, label="mc-s6"),
                                     100, 100, cw, ch, dsf1=(dsf == "on"))
            ok = inst.wait_playing(30)
            sizes = inst._sizes()
            run.log(f"dsf1={dsf}: playing={ok} inner/outer/dpr {sizes}")
            top = inst.content_inset()[1]
            for capres in a.capture_resolution.split(","):
                rec = one_capture(inst, f"dsf{dsf}-{capres}", (cw, ch), top, capres, cw, ch)
                rec.update({"dsf1": dsf, "inner_outer_dpr": sizes, "top_inset": top})
                results.append(rec)
                run.log(f"  {capres}: " + str([(d.get('png_size'), d.get('frame'), d.get('crop_ok'), d.get('psnr_db')) for d in rec["dumps"]]))
            inst.close()
        if a.oversize:
            for s in a.oversize_sizes.split(","):
                ow, oh = runner.parse_size(s)
                inst = run.launch_chrome(f"over{ow}x{oh}", run.player_url(src="/test_5min.mp4", muted=1, loop=1, label="mc-s6"),
                                         0, 0, ow, oh, dsf1=True)
                inst.wait_playing(30)
                time.sleep(1)
                iw, ih, ow_, oh_, dpr, sx, sy = inst._sizes()
                top = oh_ - ih
                run.log(f"oversize request {ow}x{oh}: inner {iw}x{ih} outer {ow_}x{oh_} dpr {dpr} pos {sx},{sy}")
                scale = min(iw / 1920, ih / 1080)
                vw, vh = round(1920 * scale), round(1080 * scale)
                vr = lambda ff, vx=(iw - round(1920 * scale)) // 2, vy=(ih - round(1080 * scale)) // 2, vw=vw, vh=vh: (vx, vy, vw, vh)
                rec = one_capture(inst, f"over{ow}x{oh}", (iw, ih), top, "automatic", iw, ih, vr)
                rec.update({"oversize_request": [ow, oh], "inner_achieved": [iw, ih], "outer": [ow_, oh_], "dpr": dpr,
                            "oversize": True})
                results.append(rec)
                inst.close()
    except KeyboardInterrupt:
        err = "interrupted"
    except Exception as e:
        err = repr(e)
        run.log("ERROR " + err)
    finally:
        run.cleanup()

    lines = []
    scale_note = ", ".join(str(s) for s in sorted(x for x in scale_seen if x is not None)) or "unknown"
    lines.append(f"S6 display backing scale (first_frame scale_factor): {scale_note}"
                 + ("  -> all displays 1x: Retina NOT tested; dsf1 on/off are equivalent here" if scale_seen <= {1, 1.0} else ""))
    lines.append("variant              out WxH    scale content_scale frame crop  blur_px PSNR dB")
    default_pass = None
    for r in results:
        ff = r.get("first_frame") or {}
        d0 = (r["dumps"] or [{}])[-1]
        psnr = d0.get("psnr_db")
        if r.get("oversize"):
            lines.append(f"oversize {r['oversize_request'][0]}x{r['oversize_request'][1]}: inner achieved {r['inner_achieved'][0]}x{r['inner_achieved'][1]} "
                         f"(outer {r['outer'][0]}x{r['outer'][1]}), capture out {ff.get('w')}x{ff.get('h')}, png {d0.get('png_size')}, "
                         f"barcode_ok={d0.get('barcode_ok')} frame={d0.get('frame')} content_rect={ff.get('content_rect')}")
            continue
        okv = (ff.get("w"), ff.get("h")) == (cw, ch) and d0.get("barcode_ok") and d0.get("crop_ok") and (psnr or 0) >= 35
        if r["dsf1"] == "on" and r["capture_resolution"] == "automatic":
            default_pass = bool(okv)
        lines.append(f"{r['tag']:<20} {ff.get('w')}x{ff.get('h')} {ff.get('scale_factor')!s:>6} {ff.get('content_scale')!s:>8}   {d0.get('frame')!s:>5} {d0.get('crop_ok')!s:<5} "
                     f"{(d0.get('blur_px') or 0):6.2f}  {psnr if psnr is None else round(psnr, 1)}{'' if okv else '  <-- check'}")
    for r in results:
        if r["errors"]:
            lines.append(f"{r['tag']}: helper errors {r['errors'][:2]}")
    if err:
        lines.append("run ended early: " + err)
    lines.append("criteria (default dsf1=on/automatic): out 1920x1080, barcode decodes, corners white (crop ok), PSNR>=35 dB")
    run.summary["results"] = results
    run.finish(lines, bool(default_pass) and not err if default_pass is not None else None)


if __name__ == "__main__":
    main()
