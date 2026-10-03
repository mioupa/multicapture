import ctypes
import threading
from ctypes import wintypes

from .win32 import (
    GUID, HRESULT, IID_IAgileObject, IID_IUnknown, check, ensure_mta, method, release,
)

mmdevapi = ctypes.WinDLL("Mmdevapi")

IID_IActivateAudioInterfaceCompletionHandler = GUID.parse("41D949AB-9862-444A-80F6-C261334DA5EB")
IID_IAudioClient = GUID.parse("1CB9AD4C-DBFA-4c32-B178-C2F568A703B2")
IID_IAudioCaptureClient = GUID.parse("C8ADBD64-E71E-48a0-A4DE-185C395CD317")

VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"
AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0
VT_BLOB = 65
AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000
AUDCLNT_BUFFERFLAGS_SILENT = 0x2
WAVE_FORMAT_PCM = 1
E_NOINTERFACE = -2147467262


class AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS(ctypes.Structure):
    _fields_ = [("TargetProcessId", wintypes.DWORD), ("ProcessLoopbackMode", ctypes.c_int)]


class AUDIOCLIENT_ACTIVATION_PARAMS(ctypes.Structure):
    _fields_ = [("ActivationType", ctypes.c_int), ("ProcessLoopbackParams", AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS)]


class BLOB(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("pBlobData", ctypes.c_void_p)]


class PROPVARIANT(ctypes.Structure):
    _fields_ = [
        ("vt", ctypes.c_ushort), ("wReserved1", ctypes.c_ushort),
        ("wReserved2", ctypes.c_ushort), ("wReserved3", ctypes.c_ushort),
        ("blob", BLOB),
    ]


class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [
        ("wFormatTag", ctypes.c_ushort), ("nChannels", ctypes.c_ushort),
        ("nSamplesPerSec", ctypes.c_uint32), ("nAvgBytesPerSec", ctypes.c_uint32),
        ("nBlockAlign", ctypes.c_ushort), ("wBitsPerSample", ctypes.c_ushort),
        ("cbSize", ctypes.c_ushort),
    ]


mmdevapi.ActivateAudioInterfaceAsync.argtypes = [
    wintypes.LPCWSTR, ctypes.POINTER(GUID), ctypes.POINTER(PROPVARIANT), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
]
mmdevapi.ActivateAudioInterfaceAsync.restype = HRESULT

_QI = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))
_REF = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
_COMPLETED = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, ctypes.c_void_p)
_VTBL = ctypes.c_void_p * 4


class _HandlerObject(ctypes.Structure):
    _fields_ = [("lpVtbl", ctypes.POINTER(_VTBL))]


class _CompletionHandler:
    def __init__(self):
        self.done = threading.Event()
        accepted = (IID_IUnknown, IID_IAgileObject, IID_IActivateAudioInterfaceCompletionHandler)

        def query(this, riid, ppv):
            if riid.contents in accepted:
                ppv[0] = this
                return 0
            ppv[0] = None
            return E_NOINTERFACE

        def completed(this, op):
            self.done.set()
            return 0

        self._callbacks = (_QI(query), _REF(lambda this: 1), _REF(lambda this: 1), _COMPLETED(completed))
        self._vtbl = _VTBL(*[ctypes.cast(cb, ctypes.c_void_p) for cb in self._callbacks])
        self._obj = _HandlerObject(ctypes.pointer(self._vtbl))

    @property
    def address(self):
        return ctypes.addressof(self._obj)


class ProcessLoopback:
    def __init__(self, pid, sample_rate=48000, channels=2):
        self.pid = pid
        self.sample_rate = sample_rate
        self.channels = channels
        self.block_align = channels * 2
        self.client = ctypes.c_void_p()
        self.capture = ctypes.c_void_p()
        self._handler = None

    def start(self, timeout=5.0):
        ensure_mta()
        params = AUDIOCLIENT_ACTIVATION_PARAMS(
            AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK,
            AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS(self.pid, PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE),
        )
        prop = PROPVARIANT()
        prop.vt = VT_BLOB
        prop.blob.cbSize = ctypes.sizeof(params)
        prop.blob.pBlobData = ctypes.cast(ctypes.pointer(params), ctypes.c_void_p)

        self._handler = _CompletionHandler()
        op = ctypes.c_void_p()
        check(mmdevapi.ActivateAudioInterfaceAsync(
            VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK, ctypes.byref(IID_IAudioClient), ctypes.byref(prop),
            self._handler.address, ctypes.byref(op),
        ), "ActivateAudioInterfaceAsync")
        try:
            if not self._handler.done.wait(timeout):
                raise TimeoutError("audio activation timed out")
            activate_hr = HRESULT()
            check(method(op, 3, HRESULT, ctypes.POINTER(HRESULT), ctypes.POINTER(ctypes.c_void_p))(
                ctypes.byref(activate_hr), ctypes.byref(self.client)
            ), "GetActivateResult")
            check(activate_hr.value, "process loopback activation")
        finally:
            release(op)

        fmt = WAVEFORMATEX(
            WAVE_FORMAT_PCM, self.channels, self.sample_rate, self.sample_rate * self.block_align,
            self.block_align, 16, 0,
        )
        flags = AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY
        check(method(self.client, 3, HRESULT, ctypes.c_int, wintypes.DWORD, ctypes.c_longlong, ctypes.c_longlong, ctypes.POINTER(WAVEFORMATEX), ctypes.c_void_p)(
            AUDCLNT_SHAREMODE_SHARED, flags, 2_000_000, 0, ctypes.byref(fmt), None
        ), "IAudioClient.Initialize")
        check(method(self.client, 14, HRESULT, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))(
            ctypes.byref(IID_IAudioCaptureClient), ctypes.byref(self.capture)
        ), "IAudioClient.GetService")
        self._get_buffer = method(self.capture, 3, HRESULT, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p, ctypes.c_void_p)
        self._release_buffer = method(self.capture, 4, HRESULT, ctypes.c_uint32)
        self._next_packet = method(self.capture, 5, HRESULT, ctypes.POINTER(ctypes.c_uint32))
        check(method(self.client, 10, HRESULT)(), "IAudioClient.Start")

    def read(self):
        chunks = []
        size = ctypes.c_uint32()
        if self._next_packet(ctypes.byref(size)) < 0:
            raise OSError("audio capture lost")
        while size.value:
            data = ctypes.c_void_p()
            frames = ctypes.c_uint32()
            flags = wintypes.DWORD()
            if self._get_buffer(ctypes.byref(data), ctypes.byref(frames), ctypes.byref(flags), None, None) < 0:
                raise OSError("audio capture lost")
            n = frames.value * self.block_align
            if flags.value & AUDCLNT_BUFFERFLAGS_SILENT or not data.value:
                chunks.append(bytes(n))
            else:
                chunks.append(ctypes.string_at(data, n))
            self._release_buffer(frames)
            if self._next_packet(ctypes.byref(size)) < 0:
                break
        return b"".join(chunks)

    def close(self):
        if self.client.value:
            method(self.client, 11, HRESULT)()
        release(self.capture)
        release(self.client)
