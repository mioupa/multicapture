import ctypes
import time
from ctypes import wintypes

from .win32 import (
    GUID, HRESULT, check, close_winrt, ensure_mta, failed, method, query_interface, release,
)

combase = ctypes.WinDLL("combase")
d3d11 = ctypes.WinDLL("d3d11")

combase.WindowsCreateString.argtypes = [wintypes.LPCWSTR, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
combase.WindowsCreateString.restype = HRESULT
combase.WindowsDeleteString.argtypes = [ctypes.c_void_p]
combase.WindowsDeleteString.restype = HRESULT
combase.RoGetActivationFactory.argtypes = [ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
combase.RoGetActivationFactory.restype = HRESULT
d3d11.D3D11CreateDevice.restype = HRESULT
d3d11.D3D11CreateDevice.argtypes = [
    ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint,
    ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint,
    ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_void_p),
]
d3d11.CreateDirect3D11DeviceFromDXGIDevice.restype = HRESULT
d3d11.CreateDirect3D11DeviceFromDXGIDevice.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]

IID_IGraphicsCaptureItemInterop = GUID.parse("3628E81B-3CAC-4C60-B7F4-23CE0E0C3356")
IID_IGraphicsCaptureItem = GUID.parse("79C3F95B-31F7-4EC2-A464-632EF5D30760")
IID_IGraphicsCaptureSession2 = GUID.parse("2c39ae40-7d2e-5044-804e-8b6799d4cf9e")
IID_IGraphicsCaptureSession3 = GUID.parse("f2cdd966-22ae-5ea1-9596-3a289344c3be")
IID_IDirect3D11CaptureFramePoolStatics2 = GUID.parse("589B103F-6BBC-5DF5-A991-02E28B3B66D5")
IID_IDirect3DDxgiInterfaceAccess = GUID.parse("A9B3D012-3DF2-4EE3-B8D1-8695F457D3C1")
IID_IDXGIDevice = GUID.parse("54EC77FA-1377-44E6-8C32-88FD5F44C84C")
IID_ID3D11Texture2D = GUID.parse("6F15AAF2-D208-4E89-9AB4-489535D34F9C")

DXGI_FORMAT_B8G8R8A8_UNORM = 87
D3D_DRIVER_TYPE_HARDWARE = 1
D3D_DRIVER_TYPE_WARP = 5
D3D11_CREATE_DEVICE_BGRA_SUPPORT = 0x20
D3D11_SDK_VERSION = 7
D3D11_USAGE_STAGING = 3
D3D11_CPU_ACCESS_READ = 0x20000
D3D11_MAP_READ = 1
POOL_BUFFERS = 4
RING_SIZE = 6


class SizeInt32(ctypes.Structure):
    _fields_ = [("Width", ctypes.c_int32), ("Height", ctypes.c_int32)]


class D3D11_TEXTURE2D_DESC(ctypes.Structure):
    _fields_ = [
        ("Width", ctypes.c_uint32), ("Height", ctypes.c_uint32),
        ("MipLevels", ctypes.c_uint32), ("ArraySize", ctypes.c_uint32),
        ("Format", ctypes.c_uint32),
        ("SampleCount", ctypes.c_uint32), ("SampleQuality", ctypes.c_uint32),
        ("Usage", ctypes.c_uint32), ("BindFlags", ctypes.c_uint32),
        ("CPUAccessFlags", ctypes.c_uint32), ("MiscFlags", ctypes.c_uint32),
    ]


class D3D11_BOX(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_uint32), ("top", ctypes.c_uint32), ("front", ctypes.c_uint32),
        ("right", ctypes.c_uint32), ("bottom", ctypes.c_uint32), ("back", ctypes.c_uint32),
    ]


class D3D11_MAPPED_SUBRESOURCE(ctypes.Structure):
    _fields_ = [("pData", ctypes.c_void_p), ("RowPitch", ctypes.c_uint32), ("DepthPitch", ctypes.c_uint32)]


def activation_factory(class_name, iid):
    hstr = ctypes.c_void_p()
    check(combase.WindowsCreateString(class_name, len(class_name), ctypes.byref(hstr)), "WindowsCreateString")
    try:
        factory = ctypes.c_void_p()
        check(combase.RoGetActivationFactory(hstr, ctypes.byref(iid), ctypes.byref(factory)), f"RoGetActivationFactory({class_name})")
        return factory
    finally:
        combase.WindowsDeleteString(hstr)


class D3DDevice:
    def __init__(self):
        ensure_mta()
        self.device = ctypes.c_void_p()
        self.context = ctypes.c_void_p()
        level = ctypes.c_int()
        hr = -1
        for driver in (D3D_DRIVER_TYPE_HARDWARE, D3D_DRIVER_TYPE_WARP):
            hr = d3d11.D3D11CreateDevice(
                None, driver, None, D3D11_CREATE_DEVICE_BGRA_SUPPORT, None, 0, D3D11_SDK_VERSION,
                ctypes.byref(self.device), ctypes.byref(level), ctypes.byref(self.context),
            )
            if not failed(hr):
                break
        check(hr, "D3D11CreateDevice")
        hr, dxgi = query_interface(self.device, IID_IDXGIDevice)
        check(hr, "QueryInterface(IDXGIDevice)")
        self.winrt_device = ctypes.c_void_p()
        check(d3d11.CreateDirect3D11DeviceFromDXGIDevice(dxgi, ctypes.byref(self.winrt_device)), "CreateDirect3D11DeviceFromDXGIDevice")
        release(dxgi)
        self._create_texture = method(self.device, 5, HRESULT, ctypes.POINTER(D3D11_TEXTURE2D_DESC), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
        self._map = method(self.context, 14, HRESULT, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(D3D11_MAPPED_SUBRESOURCE))
        self._unmap = method(self.context, 15, None, ctypes.c_void_p, ctypes.c_uint32)
        self._copy_region = method(
            self.context, 46, None,
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(D3D11_BOX),
        )

    def create_staging(self, width, height):
        desc = D3D11_TEXTURE2D_DESC(
            width, height, 1, 1, DXGI_FORMAT_B8G8R8A8_UNORM, 1, 0,
            D3D11_USAGE_STAGING, 0, D3D11_CPU_ACCESS_READ, 0,
        )
        tex = ctypes.c_void_p()
        check(self._create_texture(ctypes.byref(desc), None, ctypes.byref(tex)), "CreateTexture2D")
        return tex

    def copy_region(self, dst, src, box):
        self._copy_region(dst, 0, 0, 0, 0, src, 0, ctypes.byref(box))

    def map(self, tex):
        mapped = D3D11_MAPPED_SUBRESOURCE()
        check(self._map(tex, 0, D3D11_MAP_READ, 0, ctypes.byref(mapped)), "Map")
        return mapped

    def unmap(self, tex):
        self._unmap(tex, 0)

    def close(self):
        release(self.winrt_device)
        release(self.context)
        release(self.device)


class WindowCapture:
    def __init__(self, hwnd, device):
        self.hwnd = hwnd
        self.device = device
        self.item = ctypes.c_void_p()
        self.pool = ctypes.c_void_p()
        self.session = ctypes.c_void_p()
        self.pool_size = SizeInt32()
        self.ring = []
        self.ring_times = []
        self.ring_seq = []
        self.seq = 0
        self.head = 0
        self.last_frame_time = 0.0
        self.out_width = 0
        self.out_height = 0
        self.region = (0, 0)
        self.has_frame = False

    def start(self):
        ensure_mta()
        interop = activation_factory("Windows.Graphics.Capture.GraphicsCaptureItem", IID_IGraphicsCaptureItemInterop)
        create_for_window = method(interop, 3, HRESULT, wintypes.HWND, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))
        hr = create_for_window(self.hwnd, ctypes.byref(IID_IGraphicsCaptureItem), ctypes.byref(self.item))
        release(interop)
        check(hr, "CreateForWindow")

        method(self.item, 7, HRESULT, ctypes.POINTER(SizeInt32))(ctypes.byref(self.pool_size))

        statics = activation_factory("Windows.Graphics.Capture.Direct3D11CaptureFramePool", IID_IDirect3D11CaptureFramePoolStatics2)
        create_free_threaded = method(statics, 6, HRESULT, ctypes.c_void_p, ctypes.c_int32, ctypes.c_int32, SizeInt32, ctypes.POINTER(ctypes.c_void_p))
        hr = create_free_threaded(self.device.winrt_device, DXGI_FORMAT_B8G8R8A8_UNORM, POOL_BUFFERS, self.pool_size, ctypes.byref(self.pool))
        release(statics)
        check(hr, "CreateFreeThreaded")

        check(method(self.pool, 10, HRESULT, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(self.item, ctypes.byref(self.session)), "CreateCaptureSession")

        hr, s2 = query_interface(self.session, IID_IGraphicsCaptureSession2)
        if not failed(hr):
            method(s2, 7, HRESULT, ctypes.c_bool)(False)
            release(s2)
        hr, s3 = query_interface(self.session, IID_IGraphicsCaptureSession3)
        if not failed(hr):
            method(s3, 7, HRESULT, ctypes.c_bool)(False)
            release(s3)

        check(method(self.session, 6, HRESULT)(), "StartCapture")

    @property
    def frame_size(self):
        return self.pool_size.Width, self.pool_size.Height

    def set_output(self, width, height):
        for tex in self.ring:
            release(tex)
        self.ring = [self.device.create_staging(width, height) for _ in range(RING_SIZE)]
        self.ring_times = [float("-inf")] * RING_SIZE
        self.ring_seq = [-1] * RING_SIZE
        self.head = 0
        self.out_width = width
        self.out_height = height
        self.has_frame = False

    def _frame_time(self, frame):
        stamp = ctypes.c_int64()
        now = time.perf_counter()
        if failed(method(frame, 7, HRESULT, ctypes.POINTER(ctypes.c_int64))(ctypes.byref(stamp))):
            return now
        t = stamp.value * 1e-7
        return t if abs(t - now) < 2.0 else now

    def set_region(self, left, top):
        self.region = (max(0, left), max(0, top))

    def _texture_of(self, frame):
        surface = ctypes.c_void_p()
        method(frame, 6, HRESULT, ctypes.POINTER(ctypes.c_void_p))(ctypes.byref(surface))
        hr, access = query_interface(surface, IID_IDirect3DDxgiInterfaceAccess)
        release(surface)
        if failed(hr):
            return None
        tex = ctypes.c_void_p()
        hr = method(access, 3, HRESULT, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))(ctypes.byref(IID_ID3D11Texture2D), ctypes.byref(tex))
        release(access)
        return None if failed(hr) else tex

    def update(self):
        try_next = method(self.pool, 7, HRESULT, ctypes.POINTER(ctypes.c_void_p))
        got = False
        while True:
            frame = ctypes.c_void_p()
            if failed(try_next(ctypes.byref(frame))) or not frame.value:
                break
            self._consume(frame)
            got = True
        return got

    def _consume(self, latest):
        content = SizeInt32()
        method(latest, 8, HRESULT, ctypes.POINTER(SizeInt32))(ctypes.byref(content))
        self.last_frame_time = self._frame_time(latest)

        tex = self._texture_of(latest)
        if tex is not None:
            desc = D3D11_TEXTURE2D_DESC()
            method(tex, 10, None, ctypes.POINTER(D3D11_TEXTURE2D_DESC))(ctypes.byref(desc))
            left, top = self.region
            right = min(left + self.out_width, desc.Width, max(content.Width, 0))
            bottom = min(top + self.out_height, desc.Height, max(content.Height, 0))
            if right > left and bottom > top:
                slot = (self.head + 1) % RING_SIZE
                self.device.copy_region(self.ring[slot], tex, D3D11_BOX(left, top, 0, right, bottom, 1))
                self.ring_times[slot] = self.last_frame_time
                self.seq += 1
                self.ring_seq[slot] = self.seq
                self.head = slot
                self.has_frame = True
            release(tex)

        close_winrt(latest)
        release(latest)

        if content.Width > 0 and content.Height > 0 and (content.Width, content.Height) != self.frame_size:
            self.pool_size = SizeInt32(content.Width, content.Height)
            method(self.pool, 6, HRESULT, ctypes.c_void_p, ctypes.c_int32, ctypes.c_int32, SizeInt32)(
                self.device.winrt_device, DXGI_FORMAT_B8G8R8A8_UNORM, POOL_BUFFERS, self.pool_size
            )
        return True

    def slot_of_seq(self, seq):
        for slot in range(RING_SIZE):
            if self.ring_seq[slot] == seq:
                return slot
        return None

    def oldest_seq(self):
        valid = [s for s in self.ring_seq if s >= 0]
        return min(valid) if valid else None

    def first_seq_after(self, wall):
        candidates = [(self.ring_seq[i], self.ring_times[i]) for i in range(RING_SIZE) if self.ring_seq[i] >= 0 and self.ring_times[i] >= wall]
        return min(candidates)[0] if candidates else None

    def slot_at(self, wall):
        best = None
        for slot in range(RING_SIZE):
            if self.ring_seq[slot] >= 0 and self.ring_times[slot] <= wall:
                if best is None or self.ring_times[slot] > self.ring_times[best]:
                    best = slot
        if best is not None:
            return best
        valid = [s for s in range(RING_SIZE) if self.ring_seq[s] >= 0]
        return min(valid, key=lambda s: self.ring_times[s]) if valid else self.head

    def write_slot(self, sink, slot):
        self._write_texture(sink, self.ring[slot])

    def _write_texture(self, sink, tex):
        mapped = self.device.map(tex)
        try:
            row = self.out_width * 4
            pitch = mapped.RowPitch
            h = self.out_height
            if pitch == row:
                sink((ctypes.c_char * (row * h)).from_address(mapped.pData))
            else:
                view = memoryview((ctypes.c_char * (pitch * h)).from_address(mapped.pData)).cast("B")
                sink(b"".join(view[i * pitch:i * pitch + row] for i in range(h)))
        finally:
            self.device.unmap(tex)

    def close(self):
        close_winrt(self.session)
        release(self.session)
        close_winrt(self.pool)
        release(self.pool)
        release(self.item)
        for tex in self.ring:
            release(tex)
        self.ring = []
