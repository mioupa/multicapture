"""Local HTTP server for tools/mac-spike/media with Range support and cookie endpoints."""
import json
import mimetypes
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

from . import MEDIA_DIR

MIME = {".mp4": "video/mp4", ".html": "text/html; charset=utf-8", ".js": "text/javascript",
        ".json": "application/json", ".wav": "audio/wav", ".png": "image/png"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _remember(self, path):
        self.server.last_cookies[path] = self.headers.get("Cookie", "")

    def do_HEAD(self):
        self._serve(head=True)

    def do_GET(self):
        self._serve(head=False)

    def _send_json(self, obj, extra=()):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _serve(self, head):
        u = urlparse(self.path)
        self._remember(u.path)
        if u.path == "/setcookie":
            q = parse_qs(u.query)
            name = q.get("name", ["c"])[0]
            value = q.get("value", [""])[0]
            cookie = f"{name}={value}; Path=/"
            if q.get("max_age", [""])[0]:
                cookie += f"; Max-Age={int(q['max_age'][0])}"
            return self._send_json({"set": cookie}, [("Set-Cookie", cookie)])
        if u.path == "/whoami":
            return self._send_json({"cookie": self.headers.get("Cookie", "")})
        rel = unquote(u.path).lstrip("/") or "player.html"
        full = os.path.realpath(os.path.join(MEDIA_DIR, rel))
        if not full.startswith(os.path.realpath(MEDIA_DIR) + os.sep) or not os.path.isfile(full):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        size = os.path.getsize(full)
        ext = os.path.splitext(full)[1].lower()
        ctype = MIME.get(ext) or mimetypes.guess_type(full)[0] or "application/octet-stream"
        start, end, status = 0, size - 1, 200
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            spec = rng[6:].split(",")[0].strip()
            a, _, b = spec.partition("-")
            try:
                if a == "":
                    n = int(b)
                    start, end = max(0, size - n), size - 1
                else:
                    start = int(a)
                    end = int(b) if b else size - 1
                end = min(end, size - 1)
                if start > end or start >= size:
                    raise ValueError
                status = 206
            except ValueError:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
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

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.last_cookies = {}


def start():
    srv = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def stop(srv):
    srv.shutdown()
    srv.server_close()
