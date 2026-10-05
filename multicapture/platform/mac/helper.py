"""Session with the Swift helper `mc-capture` (protocol: native/mac/PROTOCOL.md).

One `HelperSession` wraps one `mc-capture serve` process: commands go to its stdin as JSON Lines, a
reader thread routes the events from its stdout (replies by "id", everything else is kept as state),
and its stderr goes to a log file in the log directory.
"""
import atexit
import collections
import itertools
import json
import os
import subprocess
import sys
import threading
import time

STAGGER_SECONDS = 0.5      # §5.2: start_video calls (all sessions) are spaced at least this far apart
READY_TIMEOUT = 15.0
REQUEST_TIMEOUT = 15.0

_REL_APP_HELPER = os.path.join("native", "mac", "build", "MCCapture.app", "Contents", "MacOS", "mc-capture")
_REL_BARE_HELPER = os.path.join("native", "mac", "build", "mc-capture")


class HelperError(RuntimeError):
    pass


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def helper_candidates():
    """[(argv prefix, label)] of places the helper may be, best first. Only existing files are returned."""
    found = []
    override = os.environ.get("MULTICAPTURE_HELPER")
    if override:
        # The override stands in for the source-mode helper.
        found.append(([override, "--disclaim"] if not getattr(sys, "frozen", False) else [override], "env"))
    elif getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        contents = os.path.dirname(exe_dir)
        dirs = [exe_dir, os.path.join(contents, "Frameworks"), os.path.join(contents, "Resources")]
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            dirs.insert(0, meipass)
        for d in dirs:
            found.append(([os.path.join(d, "mc-capture")], "bundle"))
    else:
        root = _repo_root()
        found.append(([os.path.join(root, _REL_APP_HELPER), "--disclaim"], "source"))
        found.append(([os.path.join(root, _REL_BARE_HELPER), "--disclaim"], "source"))
    return [(argv, label) for argv, label in found if os.path.isfile(argv[0])]


def helper_command():
    candidates = helper_candidates()
    if not candidates:
        raise HelperError(
            "録画用の補助プログラム（mc-capture）が見つかりません。"
            "native/mac のビルドが必要です（native/mac/build/MCCapture.app）。")
    return candidates[0][0] + ["serve"]


def helper_executables():
    """Absolute paths the helper may run from (used to recognise our own leftover processes)."""
    paths = {argv[0] for argv, _ in helper_candidates()}
    root = _repo_root()
    paths.update([os.path.join(root, _REL_APP_HELPER), os.path.join(root, _REL_BARE_HELPER)])
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        contents = os.path.dirname(exe_dir)
        for d in (exe_dir, os.path.join(contents, "Frameworks"), os.path.join(contents, "Resources")):
            paths.add(os.path.join(d, "mc-capture"))
    return sorted(paths)


def _log_path(log_name):
    from ...config import log_dir
    return os.path.join(log_dir(), log_name)


_stagger_lock = threading.Lock()
_last_video_start = [float("-inf")]
_sessions = set()
_sessions_lock = threading.Lock()


class _Waiter:
    __slots__ = ("event", "reply", "error")

    def __init__(self):
        self.event = threading.Event()
        self.reply = None
        self.error = None


class HelperSession:
    def __init__(self, log_name="mc-capture.log", argv=None):
        self.log_name = log_name
        self._argv = argv
        self._proc = None
        self._log = None
        self._reader = None
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._ids = itertools.count(1)
        self._pending = {}
        self.ready = threading.Event()
        self.ready_event = None
        self.users = 0
        self.closed = False
        # state fed by events
        self.events = collections.deque(maxlen=200)
        self.stats = {}
        self.stats_history = collections.deque(maxlen=30)   # (monotonic time, stats dict)
        self.stall_count = 0
        self.stalled = False
        self.stalled_since = None
        self.audio_rebuilt = []
        self.errors = []
        self.first_frame = None
        self.video_active = False
        self.video_started_at = None
        self.video_fps = 0

    # ------------------------------------------------------------------ lifecycle
    @property
    def pid(self):
        return self._proc.pid if self._proc else None

    @property
    def alive(self):
        return self._proc is not None and self._proc.poll() is None and not self.closed

    def start(self, timeout=READY_TIMEOUT):
        if self._proc is not None:
            return self
        argv = self._argv or helper_command()
        try:
            self._log = open(_log_path(self.log_name), "ab")
            self._log.write(f"\n[multicapture] session start {time.strftime('%Y-%m-%d %H:%M:%S')} argv={argv}\n".encode())
            self._log.flush()
        except OSError:
            self._log = None
        try:
            self._proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=self._log if self._log else subprocess.DEVNULL, bufsize=0)
        except OSError as exc:
            self._close_log()
            raise HelperError(f"補助プログラムを起動できませんでした: {exc}") from exc
        self._reader = threading.Thread(target=self._read_loop, name="mc-capture-reader", daemon=True)
        self._reader.start()
        with _sessions_lock:
            _sessions.add(self)
        if not self.ready.wait(timeout):
            self.close()
            raise HelperError("補助プログラムが起動しませんでした（ready が届きません）")
        if not self.alive:
            raise HelperError("補助プログラムがすぐに終了しました（ログを確認してください）")
        return self

    def _close_log(self):
        log, self._log = self._log, None
        if log:
            try:
                log.close()
            except OSError:
                pass

    def close(self, timeout=3.0):
        if self.closed:
            return
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                self.request("quit", timeout=min(timeout, 2.0))
            except HelperError:
                pass
        self.closed = True
        if proc is not None:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(2)
            if proc.stdout:
                try:
                    proc.stdout.close()
                except OSError:
                    pass
        if self._reader is not None and self._reader is not threading.current_thread():
            self._reader.join(2)
        self._fail_pending("補助プログラムが終了しました")
        self._close_log()
        with _sessions_lock:
            _sessions.discard(self)

    # ------------------------------------------------------------------ events
    def _read_loop(self):
        stream = self._proc.stdout
        buf = b""
        try:
            while True:
                chunk = stream.read(65536) if hasattr(stream, "read") else b""
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(ev, dict):
                        self._on_event(ev)
        except (OSError, ValueError):
            pass
        finally:
            self.ready.set()   # unblock start() if the process died before `ready`
            self._fail_pending("補助プログラムが終了しました")

    def _on_event(self, ev):
        kind = ev.get("ev")
        self.events.append(ev)
        log = self._log
        if log and kind not in ("windows", "permission"):
            # per-second stats and stalls end up next to the helper's own stderr for diagnosis
            try:
                log.write(b"[event] " + json.dumps(ev, ensure_ascii=False).encode() + b"\n")
                log.flush()
            except (OSError, ValueError):
                pass
        with self._state_lock:
            if kind == "ready":
                self.ready_event = ev
                self.ready.set()
            elif kind == "stats":
                self.stats = ev
                self.stats_history.append((time.monotonic(), ev))
            elif kind == "stall":
                self.stalled = True
                self.stalled_since = time.monotonic()
                self.stall_count += 1
            elif kind == "resume":
                self.stalled = False
                self.stalled_since = None
            elif kind == "audio_rebuilt":
                self.audio_rebuilt.append(ev.get("reason"))
            elif kind == "first_frame":
                self.first_frame = ev
            elif kind == "error":
                self.errors.append(ev)
        msg_id = ev.get("id")
        if msg_id is None:
            return
        with self._state_lock:
            waiter = self._pending.pop(msg_id, None)
        if waiter is None:
            return
        if kind == "error":
            waiter.error = HelperError(f"{ev.get('where', 'helper')}: {ev.get('msg', 'error')}")
            waiter.error.event = ev
        else:
            waiter.reply = ev
        waiter.event.set()

    def _fail_pending(self, message):
        with self._state_lock:
            waiters, self._pending = list(self._pending.values()), {}
        for w in waiters:
            w.error = HelperError(message)
            w.event.set()

    # ------------------------------------------------------------------ commands
    def _submit(self, cmd, args):
        if not self.alive:
            raise HelperError("補助プログラムが動いていません")
        msg_id = next(self._ids)
        waiter = _Waiter()
        with self._state_lock:
            self._pending[msg_id] = waiter
        data = (json.dumps({"cmd": cmd, "id": msg_id, **args}, separators=(",", ":")) + "\n").encode()
        try:
            with self._write_lock:
                if cmd == "start_video":
                    self._stagger_and_write(data)
                else:
                    self._proc.stdin.write(data)
        except (OSError, ValueError) as exc:
            with self._state_lock:
                self._pending.pop(msg_id, None)
            raise HelperError(f"補助プログラムに命令を送れませんでした: {exc}") from exc
        return msg_id, waiter

    def _stagger_and_write(self, data):
        with _stagger_lock:
            wait = _last_video_start[0] + STAGGER_SECONDS - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._proc.stdin.write(data)
            _last_video_start[0] = time.monotonic()

    def request(self, cmd, timeout=REQUEST_TIMEOUT, **args):
        """Send a command and wait for the event carrying its id; raises HelperError on `error`/timeout."""
        msg_id, waiter = self._submit(cmd, args)
        if not waiter.event.wait(timeout):
            with self._state_lock:
                self._pending.pop(msg_id, None)
            raise HelperError(f"補助プログラムの応答がありません（{cmd}）")
        if waiter.error is not None:
            raise waiter.error
        return waiter.reply

    # ------------------------------------------------------------------ typed helpers
    def start_video(self, window_id, width, height, crop, fps, shm, timeout=30.0):
        reply = self.request("start_video", timeout=timeout, window_id=window_id, width=int(width), height=int(height),
                             crop=[int(v) for v in crop], fps=int(fps), shm=shm)
        self.video_active = True
        self.video_started_at = time.monotonic()
        self.video_fps = int(fps)
        return reply

    def update_video(self, crop, timeout=10.0):
        return self.request("update_video", timeout=timeout, crop=[int(v) for v in crop])

    def stop_video(self, timeout=5.0):
        self.video_active = False
        return self.request("stop_video", timeout=timeout)

    def start_audio(self, tree_pid, mute, fifo, timeout=5.0):
        return self.request("start_audio", timeout=timeout, tree_pid=int(tree_pid), mute=mute, fifo=fifo)

    def stop_audio(self, timeout=5.0):
        return self.request("stop_audio", timeout=timeout)


# ---------------------------------------------------------------------- shared per-owner sessions
_owner_lock = threading.Lock()


def acquire_session(owner, log_name):
    """The helper session attached to `owner` (a BrowserWindow), started on demand; counts one user."""
    with _owner_lock:
        session = getattr(owner, "helper_session", None)
        if session is None or not session.alive:
            session = HelperSession(log_name).start()
            owner.helper_session = session
        session.users += 1
        return session


def release_session(owner, session):
    """Drop one user; the last one quits the helper."""
    with _owner_lock:
        session.users -= 1
        if session.users > 0:
            return
        if getattr(owner, "helper_session", None) is session:
            owner.helper_session = None
    session.close()


# ---------------------------------------------------------------------- control session
_control = [None]
_control_lock = threading.Lock()


def control_session():
    with _control_lock:
        session = _control[0]
        if session is None or not session.alive:
            session = HelperSession("mc-capture_control.log").start()
            _control[0] = session
        return session


def _close_control():
    session, _control[0] = _control[0], None
    if session is not None:
        session.close(timeout=2.0)


atexit.register(_close_control)


def check_permission():
    ev = control_session().request("check_permission", timeout=10)
    return {"screen": bool(ev.get("screen")), "audio": ev.get("audio", "unknown")}


def request_permission():
    ev = control_session().request("request_permission", timeout=75)
    return {"screen": bool(ev.get("screen")), "audio": ev.get("audio", "unknown")}


def list_windows(pid):
    ev = control_session().request("list_windows", timeout=15, pid=int(pid))
    return ev.get("windows", [])


def pick_window(windows):
    """The on-screen, layer-0 window with the largest frame."""
    best, best_area = None, -1
    for w in windows:
        if not w.get("on_screen", True) or w.get("layer", 0) != 0:
            continue
        frame = w.get("frame") or [0, 0, 0, 0]
        area = frame[2] * frame[3]
        if area > best_area:
            best, best_area = w, area
    return best["window_id"] if best else None


def active_sessions():
    with _sessions_lock:
        return list(_sessions)
