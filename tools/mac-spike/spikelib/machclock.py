"""mach_absolute_time / mach_continuous_time via ctypes (seconds)."""
import ctypes
import ctypes.util

_lib = ctypes.CDLL(ctypes.util.find_library("System") or "/usr/lib/libSystem.B.dylib")


class _Timebase(ctypes.Structure):
    _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]


_lib.mach_absolute_time.restype = ctypes.c_uint64
_lib.mach_continuous_time.restype = ctypes.c_uint64
_tb = _Timebase()
_lib.mach_timebase_info(ctypes.byref(_tb))
_SCALE = _tb.numer / _tb.denom * 1e-9


def now():
    return _lib.mach_absolute_time() * _SCALE


def continuous():
    return _lib.mach_continuous_time() * _SCALE


if __name__ == "__main__":
    print(now(), continuous(), _tb.numer, _tb.denom)
