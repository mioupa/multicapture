import base64
import json
import os
import socket
import struct
import threading
import time
from urllib.parse import urlparse


class CDPError(RuntimeError):
    pass


class WebSocket:
    def __init__(self, url, timeout=15.0):
        parsed = urlparse(url)
        self.sock = socket.create_connection((parsed.hostname, parsed.port or 80), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        request = (
            f"GET {path} HTTP/1.1\r\nHost: {parsed.hostname}:{parsed.port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode())
        header = b""
        while b"\r\n\r\n" not in header:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise CDPError("WebSocket handshake failed")
            header += chunk
        head, self._buffer = header.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise CDPError("WebSocket handshake rejected")

    def _read(self, n):
        while len(self._buffer) < n:
            chunk = self.sock.recv(max(65536, n - len(self._buffer)))
            if not chunk:
                raise CDPError("connection closed")
            self._buffer += chunk
        data, self._buffer = self._buffer[:n], self._buffer[n:]
        return data

    def _send_frame(self, opcode, payload):
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        if n:
            keyed = (mask * (n // 4 + 1))[:n]
            payload = (int.from_bytes(payload, "big") ^ int.from_bytes(keyed, "big")).to_bytes(n, "big")
        self.sock.sendall(bytes(header) + payload)

    def send(self, text):
        self._send_frame(0x1, text.encode("utf-8"))

    def recv(self):
        message = b""
        while True:
            b1, b2 = self._read(2)
            opcode = b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b2 & 0x80 else None
            payload = self._read(n)
            if mask:
                keyed = (mask * (n // 4 + 1))[:n]
                payload = (int.from_bytes(payload, "big") ^ int.from_bytes(keyed, "big")).to_bytes(n, "big")
            if opcode == 0x8:
                raise CDPError("connection closed")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            message += payload
            if b1 & 0x80:
                return message.decode("utf-8", errors="replace")

    def settimeout(self, timeout):
        self.sock.settimeout(timeout)

    def close(self):
        try:
            self._send_frame(0x8, b"")
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def read_devtools_port(profile_dir, timeout=20.0):
    path = os.path.join(profile_dir, "DevToolsActivePort")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.read().split()
            if len(lines) >= 2:
                return f"ws://127.0.0.1:{int(lines[0])}{lines[1]}"
        except (OSError, ValueError):
            pass
        time.sleep(0.2)
    raise CDPError("ブラウザの制御ポートが見つかりませんでした")


class Browser:
    def __init__(self, ws_url, timeout=15.0):
        self.ws = WebSocket(ws_url, timeout)
        self._next_id = 0
        self._lock = threading.Lock()
        self.events = []

    def call(self, method, params=None, session_id=None, timeout=15.0):
        with self._lock:
            self._next_id += 1
            msg_id = self._next_id
            message = {"id": msg_id, "method": method, "params": params or {}}
            if session_id:
                message["sessionId"] = session_id
            self.ws.send(json.dumps(message))
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CDPError(f"{method}: timeout")
                self.ws.settimeout(remaining)
                try:
                    data = json.loads(self.ws.recv())
                except socket.timeout:
                    raise CDPError(f"{method}: timeout")
                if data.get("id") == msg_id:
                    if "error" in data:
                        raise CDPError(f"{method}: {data['error'].get('message')}")
                    return data.get("result", {})
                if "method" in data and len(self.events) < 1000:
                    self.events.append(data)

    def targets(self):
        return self.call("Target.getTargets").get("targetInfos", [])

    def attach(self, target_id):
        return self.call("Target.attachToTarget", {"targetId": target_id, "flatten": True})["sessionId"]

    def evaluate(self, session_id, expression, timeout=15.0, user_gesture=False):
        result = self.call("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
            "userGesture": user_gesture,
        }, session_id=session_id, timeout=timeout)
        if "exceptionDetails" in result:
            raise CDPError(result["exceptionDetails"].get("text", "script error"))
        return result.get("result", {}).get("value")

    def get_cookies(self):
        return self.call("Storage.getCookies").get("cookies", [])

    def set_cookies(self, cookies):
        allowed = ("name", "value", "domain", "path", "secure", "httpOnly", "sameSite", "priority", "partitionKey")
        params = []
        for c in cookies:
            p = {k: c[k] for k in allowed if k in c and c[k] is not None}
            if not c.get("session") and c.get("expires", -1) > 0:
                p["expires"] = c["expires"]
            params.append(p)
        if params:
            self.call("Storage.setCookies", {"cookies": params})

    def close(self):
        self.ws.close()
