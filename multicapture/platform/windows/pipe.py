import ctypes
import uuid
from ctypes import wintypes

from . import win32

kernel32 = win32.kernel32
kernel32.CreateNamedPipeW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
]
kernel32.CreateNamedPipeW.restype = wintypes.HANDLE
kernel32.ConnectNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
kernel32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
kernel32.DisconnectNamedPipe.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

PIPE_ACCESS_OUTBOUND = 0x00000002
PIPE_TYPE_BYTE = 0x00000000
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
ERROR_PIPE_CONNECTED = 535


class AudioPipe:
    def __init__(self):
        self.path = rf"\\.\pipe\multicapture-{uuid.uuid4().hex}"
        self.handle = kernel32.CreateNamedPipeW(self.path, PIPE_ACCESS_OUTBOUND, PIPE_TYPE_BYTE, 1, 1 << 20, 0, 0, None)
        if self.handle == INVALID_HANDLE_VALUE:
            raise OSError("名前付きパイプを作成できませんでした")

        self.connected = False

    def connect(self):
        if not kernel32.ConnectNamedPipe(self.handle, None) and ctypes.get_last_error() != ERROR_PIPE_CONNECTED:
            raise OSError("FFmpegが音声パイプに接続しませんでした")
        self.connected = True

    def write(self, data):
        base = ctypes.cast(ctypes.c_char_p(data), ctypes.c_void_p).value
        offset = 0
        while offset < len(data):
            written = wintypes.DWORD()
            if not kernel32.WriteFile(self.handle, base + offset, len(data) - offset, ctypes.byref(written), None):
                raise BrokenPipeError("audio pipe closed")
            offset += written.value

    def close(self):
        if self.handle and not self.connected:
            try:
                open(self.path, "rb").close()
            except OSError:
                pass
        if self.handle and self.handle != INVALID_HANDLE_VALUE:
            kernel32.FlushFileBuffers(self.handle)
            kernel32.CloseHandle(self.handle)
            self.handle = None
