"""Launch the Swift helper mc-spike and parse its JSON Lines output."""
import json
import os
import signal
import subprocess
import threading
import time

from . import SPIKE_DIR

HELPER = os.environ.get("MC_SPIKE_HELPER") or os.path.join(SPIKE_DIR, "build", "MCSpike.app", "Contents", "MacOS", "mc-spike")


def run_once(args, timeout=30):
    """Run a one-shot helper command; return list of parsed JSON events."""
    p = subprocess.run([HELPER, *map(str, args)], capture_output=True, text=True, timeout=timeout)
    out = []
    for line in p.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            out.append({"ev": "unparsed", "line": line})
    return out


class Helper:
    def __init__(self, args, label="", stderr_path=None, on_event=None):
        self.args = [HELPER, *map(str, args)]
        if label:
            self.args += ["--label", label]
        self.label = label
        self.events = []
        self.on_event = on_event
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._stderr = open(stderr_path, "wb") if stderr_path else subprocess.DEVNULL
        self.proc = subprocess.Popen(self.args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=self._stderr)
        self.t_start = time.monotonic()
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self):
        for raw in self.proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                ev = {"ev": "unparsed", "line": line}
            ev.setdefault("_recv", time.monotonic())
            with self._cv:
                self.events.append(ev)
                self._cv.notify_all()
            if self.on_event:
                try:
                    self.on_event(self, ev)
                except Exception:
                    pass
        with self._cv:
            self._cv.notify_all()

    def snapshot(self):
        with self._lock:
            return list(self.events)

    def find(self, ev):
        return [e for e in self.snapshot() if e.get("ev") == ev]

    def wait_for(self, ev, timeout=15.0):
        """Wait until an event named `ev` has arrived; return it, or None on timeout/exit."""
        deadline = time.monotonic() + timeout
        with self._cv:
            while True:
                for e in self.events:
                    if e.get("ev") == ev:
                        return e
                if self.proc.poll() is not None and not self._thread.is_alive():
                    return None
                rem = deadline - time.monotonic()
                if rem <= 0:
                    return None
                self._cv.wait(min(rem, 0.5))

    def alive(self):
        return self.proc.poll() is None

    def stop(self, timeout=5.0):
        p = self.proc
        try:
            if p.stdin:
                p.stdin.close()
        except OSError:
            pass
        try:
            p.wait(timeout)
        except subprocess.TimeoutExpired:
            p.send_signal(signal.SIGTERM)
            try:
                p.wait(2)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        self._thread.join(2)
        if self._stderr not in (None, subprocess.DEVNULL):
            try:
                self._stderr.close()
            except OSError:
                pass
        return p.returncode
