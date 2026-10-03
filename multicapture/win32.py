import ctypes
import uuid
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
ole32 = ctypes.WinDLL("ole32")
dwmapi = ctypes.WinDLL("dwmapi")

HRESULT = ctypes.c_long
S_OK = 0


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def parse(cls, text):
        u = uuid.UUID(text)
        g = cls()
        g.Data1, g.Data2, g.Data3 = u.fields[0], u.fields[1], u.fields[2]
        g.Data4 = (ctypes.c_ubyte * 8)(*u.bytes[8:])
        return g

    def __eq__(self, other):
        return bytes(self) == bytes(other)

    def __hash__(self):
        return hash(bytes(self))


IID_IUnknown = GUID.parse("00000000-0000-0000-C000-000000000046")
IID_IAgileObject = GUID.parse("94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90")
IID_IClosable = GUID.parse("30D5A829-7FA4-4026-83BB-D75BAE4EA99E")


class ComError(OSError):
    pass


def failed(hr):
    return hr < 0


def check(hr, what):
    if hr < 0:
        raise ComError(f"{what} failed (0x{hr & 0xFFFFFFFF:08X})")
    return hr


_prototypes = {}


def method(ptr, index, restype, *argtypes):
    key = (restype, argtypes)
    proto = _prototypes.get(key)
    if proto is None:
        proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
        _prototypes[key] = proto
    table = ctypes.cast(ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p * (index + 1))).contents
    fn = proto(table[index])
    return lambda *args: fn(ptr, *args)


def query_interface(ptr, iid):
    out = ctypes.c_void_p()
    hr = method(ptr, 0, HRESULT, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))(ctypes.byref(iid), ctypes.byref(out))
    return hr, out


def release(ptr):
    if ptr is not None and ptr.value:
        method(ptr, 2, ctypes.c_ulong)()
        ptr.value = None


def close_winrt(ptr):
    if ptr is None or not ptr.value:
        return
    hr, closable = query_interface(ptr, IID_IClosable)
    if not failed(hr):
        method(closable, 6, HRESULT)()
        release(closable)


ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
ole32.CoInitializeEx.restype = HRESULT


def ensure_mta():
    ole32.CoInitializeEx(None, 0)


def set_dpi_awareness():
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        pass


user32.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
user32.EnumChildWindows.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.LPARAM]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]

_ENUM_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SW_SHOWNOACTIVATE = 4
WM_CLOSE = 0x0010
DWMWA_EXTENDED_FRAME_BOUNDS = 9


def class_name(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def window_pid(hwnd):
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def top_level_windows():
    found = []

    def cb(hwnd, _):
        found.append(hwnd)
        return True

    user32.EnumWindows(_ENUM_PROC(cb), 0)
    return found


def child_windows(hwnd):
    found = []

    def cb(child, _):
        found.append(child)
        return True

    user32.EnumChildWindows(hwnd, _ENUM_PROC(cb), 0)
    return found


def window_rect(hwnd):
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def frame_bounds(hwnd):
    r = wintypes.RECT()
    if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r), ctypes.sizeof(r)) != S_OK:
        return window_rect(hwnd)
    return r.left, r.top, r.right, r.bottom


def is_window(hwnd):
    return bool(user32.IsWindow(hwnd))


def restore_if_minimized(hwnd):
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)


def move_window(hwnd, x, y, w, h):
    user32.SetWindowPos(hwnd, None, x, y, w, h, SWP_NOZORDER | SWP_NOACTIVATE)


def close_window(hwnd):
    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)


def primary_screen_size():
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]


def process_tree(root_pid):
    snap = kernel32.CreateToolhelp32Snapshot(0x2, 0)
    if not snap or snap == wintypes.HANDLE(-1).value:
        return {root_pid}
    parents = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            parents[entry.th32ProcessID] = entry.th32ParentProcessID
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    tree = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, ppid in parents.items():
            if ppid in tree and pid not in tree and pid != ppid:
                tree.add(pid)
                changed = True
    return tree


class REASON_CONTEXT(ctypes.Structure):
    _fields_ = [
        ("Version", ctypes.c_ulong),
        ("Flags", wintypes.DWORD),
        ("SimpleReasonString", ctypes.c_wchar_p),
        ("_reserved1", ctypes.c_ulong),
        ("_reserved2", ctypes.c_ulong),
        ("_reserved3", ctypes.c_void_p),
    ]


POWER_REQUEST_CONTEXT_VERSION = 0
POWER_REQUEST_CONTEXT_SIMPLE_STRING = 0x1
POWER_REQUEST_DISPLAY_REQUIRED = 0
POWER_REQUEST_SYSTEM_REQUIRED = 1
POWER_REQUEST_AWAY_MODE_REQUIRED = 2
POWER_REQUEST_EXECUTION_REQUIRED = 3

kernel32.PowerCreateRequest.argtypes = [ctypes.POINTER(REASON_CONTEXT)]
kernel32.PowerCreateRequest.restype = wintypes.HANDLE
kernel32.PowerSetRequest.argtypes = [wintypes.HANDLE, ctypes.c_int]
kernel32.PowerSetRequest.restype = wintypes.BOOL
kernel32.PowerClearRequest.argtypes = [wintypes.HANDLE, ctypes.c_int]
kernel32.PowerClearRequest.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


class KeepAwake:
    def __init__(self, reason):
        self._reason = reason
        self._handle = None
        self._types = []

    def acquire(self, keep_display=True):
        if self._handle:
            return True
        context = REASON_CONTEXT(POWER_REQUEST_CONTEXT_VERSION, POWER_REQUEST_CONTEXT_SIMPLE_STRING, self._reason)
        handle = kernel32.PowerCreateRequest(ctypes.byref(context))
        if not handle or handle == wintypes.HANDLE(-1).value:
            return False
        types = [POWER_REQUEST_SYSTEM_REQUIRED, POWER_REQUEST_EXECUTION_REQUIRED]
        if keep_display:
            types.append(POWER_REQUEST_DISPLAY_REQUIRED)
        self._types = [t for t in types if kernel32.PowerSetRequest(handle, t)]
        self._handle = handle
        return POWER_REQUEST_SYSTEM_REQUIRED in self._types

    def release(self):
        if not self._handle:
            return
        for t in self._types:
            kernel32.PowerClearRequest(self._handle, t)
        kernel32.CloseHandle(self._handle)
        self._handle = None
        self._types = []

    @property
    def active_types(self):
        return list(self._types)
