#!/usr/bin/env python3
"""Serve tools/verify/media/ on 127.0.0.1 with HTTP Range (206) support, plus a player page.

  python3 tools/verify/serve.py [--port 8765]
  open  http://127.0.0.1:8765/?src=test_5min.mp4      (index.html: one <video controls autoplay>)

Query params of the page: src (default test_5min.mp4), muted=1 (default 0), loop=1,
vol=0..1 (element volume; e.g. vol=0.05 keeps tests quiet while still recording the beeps).
The page has exactly one <video> and no overlays, so the app's "動画を高速録画" can find it
(duration > 0 after metadata load) and pin it full-window.
"""
import argparse
import mimetypes
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
MEDIA = os.path.join(HERE, "media")

INDEX = """<!doctype html>
<html><head><meta charset="utf-8"><title>MultiCapture test video</title>
<style>html,body{margin:0;background:#000;height:100%}video{width:100vw;height:100vh;object-fit:contain;background:#000}</style>
</head><body>
<video id="v" controls autoplay playsinline preload="auto"></video>
<script>
var q = new URLSearchParams(location.search), v = document.getElementById('v');
v.muted = q.get('muted') === '1'; v.loop = q.get('loop') === '1';
var vol = parseFloat(q.get('vol')); if (vol >= 0 && vol <= 1) v.volume = vol;
v.src = q.get('src') || 'test_5min.mp4';
var p = v.play(); if (p && p.catch) p.catch(function () {});  // autoplay with sound may need a click
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if self.server.verbose:
            sys.stderr.write("%s %s\n" % (self.command, self.path))

    def do_HEAD(self):
        self._serve(True)

    def do_GET(self):
        self._serve(False)

    def _bytes(self, body, ctype, head):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _empty(self, code, extra=()):
        self.send_response(code)
        for k, v in extra:
            self.send_header(k, v)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _serve(self, head):
        rel = unquote(urlparse(self.path).path).lstrip("/")
        if rel in ("", "index.html"):
            return self._bytes(INDEX.encode(), "text/html; charset=utf-8", head)
        full = os.path.realpath(os.path.join(MEDIA, rel))
        if not full.startswith(os.path.realpath(MEDIA) + os.sep) or not os.path.isfile(full):
            return self._empty(404)
        size = os.path.getsize(full)
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        start, end, status = 0, size - 1, 200
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            a, _, b = rng[6:].split(",")[0].strip().partition("-")
            try:
                if a == "":
                    start, end = max(0, size - int(b)), size - 1
                else:
                    start = int(a)
                    end = min(int(b), size - 1) if b else size - 1
                if start > end or start >= size:
                    raise ValueError
                status = 206
            except ValueError:
                return self._empty(416, [("Content-Range", f"bytes */{size}")])
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if head:
            return
        try:
            with open(full, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    verbose = False


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    os.makedirs(MEDIA, exist_ok=True)
    srv = Server(("127.0.0.1", a.port), Handler)
    srv.verbose = a.verbose
    files = sorted(f for f in os.listdir(MEDIA) if f.endswith(".mp4"))
    print(f"serving {MEDIA} on http://127.0.0.1:{a.port}/  files: {', '.join(files) or '(none)'}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
