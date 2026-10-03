import ctypes
import json
import os
from ctypes import wintypes

from .win32 import GUID, HRESULT, ensure_mta, failed, method, ole32, release

CLSID_MMDeviceEnumerator = GUID.parse("BCDE0395-E52F-467C-8E3D-C4579291692E")
IID_IMMDeviceEnumerator = GUID.parse("A95664D2-9614-4F35-A746-DE8DB63617E6")
IID_IAudioEndpointVolume = GUID.parse("5CDF2C82-841E-4546-9722-0CF74078229A")
CLSCTX_ALL = 0x17
E_RENDER = 0
DEVICE_STATE_ACTIVE = 1

ole32.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
ole32.CoCreateInstance.restype = HRESULT
ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]


def _endpoints():
    ensure_mta()
    result = []
    enum = ctypes.c_void_p()
    if failed(ole32.CoCreateInstance(ctypes.byref(CLSID_MMDeviceEnumerator), None, CLSCTX_ALL, ctypes.byref(IID_IMMDeviceEnumerator), ctypes.byref(enum))):
        return result
    collection = ctypes.c_void_p()
    try:
        if failed(method(enum, 3, HRESULT, ctypes.c_int, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p))(E_RENDER, DEVICE_STATE_ACTIVE, ctypes.byref(collection))):
            return result
        count = ctypes.c_uint()
        method(collection, 3, HRESULT, ctypes.POINTER(ctypes.c_uint))(ctypes.byref(count))
        for i in range(count.value):
            device = ctypes.c_void_p()
            method(collection, 4, HRESULT, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p))(i, ctypes.byref(device))
            text = ctypes.c_wchar_p()
            method(device, 5, HRESULT, ctypes.POINTER(ctypes.c_wchar_p))(ctypes.byref(text))
            device_id = text.value or ""
            ole32.CoTaskMemFree(ctypes.cast(text, ctypes.c_void_p))
            volume = ctypes.c_void_p()
            hr = method(device, 3, HRESULT, ctypes.POINTER(GUID), wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(
                ctypes.byref(IID_IAudioEndpointVolume), CLSCTX_ALL, None, ctypes.byref(volume))
            release(device)
            if not failed(hr):
                result.append((device_id, volume))
    finally:
        release(collection)
        release(enum)
    return result


def _get_mute(volume):
    value = wintypes.BOOL()
    method(volume, 15, HRESULT, ctypes.POINTER(wintypes.BOOL))(ctypes.byref(value))
    return bool(value.value)


def _set_mute(volume, mute):
    method(volume, 14, HRESULT, wintypes.BOOL, ctypes.c_void_p)(bool(mute), None)


class SpeakerMute:
    def __init__(self, state_path):
        self.state_path = state_path
        self.active = False

    def mute(self):
        if self.active:
            return
        original = {}
        endpoints = _endpoints()
        for device_id, volume in endpoints:
            try:
                original[device_id] = _get_mute(volume)
            except OSError:
                pass
        try:
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump(original, f)
        except OSError:
            pass
        for device_id, volume in endpoints:
            try:
                if device_id in original and not original[device_id]:
                    _set_mute(volume, True)
            finally:
                release(volume)
        self.active = True

    def restore(self):
        try:
            with open(self.state_path, encoding="utf-8") as f:
                original = json.load(f)
        except (OSError, ValueError):
            self.active = False
            return
        for device_id, volume in _endpoints():
            try:
                if device_id in original:
                    _set_mute(volume, original[device_id])
            finally:
                release(volume)
        try:
            os.remove(self.state_path)
        except OSError:
            pass
        self.active = False
