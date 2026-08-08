from __future__ import annotations

import asyncio
import ctypes
import time
import uuid
from collections.abc import Sequence
from contextlib import suppress
from ctypes import wintypes
from typing import Any

import numpy as np
from winrt.windows.graphics import SizeInt32
from winrt.windows.graphics.capture import Direct3D11CaptureFramePool
from winrt.windows.graphics.capture.interop import create_for_window
from winrt.windows.graphics.directx import DirectXPixelFormat
from winrt.windows.graphics.directx.direct3d11.interop import (
    create_direct3d11_device_from_dxgi_device,
)
from winrt.windows.graphics.imaging import SoftwareBitmap
from winrt.windows.storage.streams import Buffer

from summarize_meeting.capture.screen.base import (
    BgrFrame,
    ScreenTargetClosedError,
    ScreenTargetPausedError,
)
from summarize_meeting.capture.screen.windows_mss import WindowsMssScreenBackend
from summarize_meeting.domain.capture import ScreenTarget

_D3D_DRIVER_TYPE_HARDWARE = 1
_D3D11_CREATE_DEVICE_BGRA_SUPPORT = 0x20
_D3D11_SDK_VERSION = 7
_IID_IDXGI_DEVICE = "54EC77FA-1377-44E6-8C32-88FD5F44C84C"
_PIXEL_FORMAT = DirectXPixelFormat.B8_G8_R8_A8_UINT_NORMALIZED
_FRAME_WAIT_SECONDS = 2.0
_FRAME_POLL_SECONDS = 0.05
_HRESULT = ctypes.c_long

user32 = ctypes.windll.user32
dwmapi = ctypes.windll.dwmapi


class _Rect(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class _Guid(ctypes.Structure):
    _fields_ = [
        ("data1", wintypes.DWORD),
        ("data2", wintypes.WORD),
        ("data3", wintypes.WORD),
        ("data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def parse(cls, value: str) -> _Guid:
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


def _com_method(
    pointer: ctypes.c_void_p,
    index: int,
    restype: type[ctypes._SimpleCData],
    *argtypes: type[object],
) -> Any:
    vtable = ctypes.cast(
        pointer,
        ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)),
    ).contents
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtable[index])


def _release(pointer: ctypes.c_void_p) -> None:
    if pointer:
        _com_method(pointer, 2, wintypes.ULONG)(pointer)


def _raise_for_hresult(result: int, operation: str) -> None:
    if result < 0:
        unsigned_result = result & 0xFFFFFFFF
        raise OSError(result, f"{operation} (HRESULT 0x{unsigned_result:08X})")


def _create_direct3d_device() -> object:
    create_device = ctypes.windll.d3d11.D3D11CreateDevice
    create_device.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.UINT,
        ctypes.c_void_p,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    create_device.restype = _HRESULT

    device = ctypes.c_void_p()
    context = ctypes.c_void_p()
    dxgi_device = ctypes.c_void_p()
    try:
        result = create_device(
            None,
            _D3D_DRIVER_TYPE_HARDWARE,
            None,
            _D3D11_CREATE_DEVICE_BGRA_SUPPORT,
            None,
            0,
            _D3D11_SDK_VERSION,
            ctypes.byref(device),
            None,
            ctypes.byref(context),
        )
        _raise_for_hresult(result, "D3D11CreateDevice failed")
        query_interface = _com_method(
            device,
            0,
            _HRESULT,
            ctypes.POINTER(_Guid),
            ctypes.POINTER(ctypes.c_void_p),
        )
        interface_id = _Guid.parse(_IID_IDXGI_DEVICE)
        result = query_interface(
            device,
            ctypes.byref(interface_id),
            ctypes.byref(dxgi_device),
        )
        _raise_for_hresult(result, "ID3D11Device.QueryInterface(IDXGIDevice) failed")
        return create_direct3d11_device_from_dxgi_device(dxgi_device.value)
    finally:
        _release(dxgi_device)
        _release(context)
        _release(device)


def _bgra_buffer_to_bgr(buffer: Buffer, width: int, height: int) -> BgrFrame:
    expected_size = width * height * 4
    if buffer.length != expected_size:
        raise RuntimeError(
            f"Unexpected WGC frame buffer size: {buffer.length} (expected {expected_size})"
        )
    bgra = np.frombuffer(memoryview(buffer), dtype=np.uint8).reshape(height, width, 4)
    return np.ascontiguousarray(bgra[:, :, :3])


async def _copy_surface(surface: object) -> SoftwareBitmap:
    return await SoftwareBitmap.create_copy_from_surface_async(surface)


class WindowsWgcScreenBackend:
    """Capture an HWND through Windows.Graphics.Capture on Windows 11."""

    def __init__(self) -> None:
        self._target_id: str | None = None
        self._device: object | None = None
        self._item: object | None = None
        self._frame_pool: object | None = None
        self._session: object | None = None
        self._pool_size: tuple[int, int] | None = None
        self._last_frame: BgrFrame | None = None

    def list_targets(self) -> Sequence[ScreenTarget]:
        return WindowsMssScreenBackend().list_targets()

    def capture(self, target: ScreenTarget) -> BgrFrame:
        hwnd = int(target.id)
        self._validate_target(hwnd)
        if target.id != self._target_id:
            self._start_capture(hwnd, target.id)
        else:
            self._sync_pool_to_window_size(hwnd)

        wait_seconds = _FRAME_WAIT_SECONDS if self._last_frame is None else _FRAME_POLL_SECONDS
        deadline = time.monotonic() + wait_seconds
        converted = None
        while converted is None:
            frame = self._wait_for_frame(hwnd, max(0.0, deadline - time.monotonic()))
            if frame is None:
                self._validate_target(hwnd)
                if self._last_frame is not None:
                    return self._last_frame.copy()
                raise RuntimeError("Windows Graphics Captureのフレーム取得がタイムアウトしました")
            converted = self._convert_frame(frame)

        result, content_size, width, height = converted

        if self._pool_size != (width, height):
            self._recreate_pool(content_size)
        self._last_frame = result
        return result

    def close(self) -> None:
        for name in ("_session", "_frame_pool", "_item", "_device"):
            resource = getattr(self, name)
            if resource is not None:
                setattr(self, name, None)
                close = getattr(resource, "close", None)
                if close is not None:
                    with suppress(OSError):
                        close()
        self._target_id = None
        self._pool_size = None
        self._last_frame = None

    def _start_capture(self, hwnd: int, target_id: str) -> None:
        self.close()
        try:
            self._device = _create_direct3d_device()
            self._item = create_for_window(hwnd)
            size = self._item.size
            if size.width <= 0 or size.height <= 0:
                raise ScreenTargetPausedError("選択したウィンドウの表示領域がありません")
            self._frame_pool = Direct3D11CaptureFramePool.create_free_threaded(
                self._device,
                _PIXEL_FORMAT,
                2,
                size,
            )
            self._session = self._frame_pool.create_capture_session(self._item)
            self._session.is_cursor_capture_enabled = False
            self._session.is_border_required = False
            self._session.start_capture()
            self._target_id = target_id
            self._pool_size = (int(size.width), int(size.height))
        except Exception:
            self.close()
            raise

    def _wait_for_frame(self, hwnd: int, wait_seconds: float) -> object | None:
        if self._frame_pool is None:
            raise RuntimeError("WGC frame pool is not initialized")
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            frame = self._frame_pool.try_get_next_frame()
            if frame is not None:
                current_size = self._window_size(hwnd)
                frame_size = (int(frame.content_size.width), int(frame.content_size.height))
                if all(current_size) and frame_size != current_size:
                    frame.close()
                    time.sleep(0.01)
                    continue
                return frame
            time.sleep(0.01)
        return None

    @staticmethod
    def _convert_frame(frame: object) -> tuple[BgrFrame, object, int, int] | None:
        content_size = frame.content_size
        width = int(content_size.width)
        height = int(content_size.height)
        if width <= 0 or height <= 0:
            frame.close()
            raise ScreenTargetPausedError("選択したウィンドウの表示領域がありません")

        surface = frame.surface
        try:
            bitmap = asyncio.run(_copy_surface(surface))
            try:
                bitmap_size = (int(bitmap.pixel_width), int(bitmap.pixel_height))
                if bitmap_size != (width, height):
                    return None
                buffer = Buffer(width * height * 4)
                bitmap.copy_to_buffer(buffer)
                result = _bgra_buffer_to_bgr(buffer, width, height)
                return result, content_size, width, height
            finally:
                bitmap.close()
        finally:
            surface.close()
            frame.close()

    def _recreate_pool(self, size: object) -> None:
        if self._frame_pool is None or self._device is None:
            return
        self._frame_pool.recreate(self._device, _PIXEL_FORMAT, 2, size)
        self._pool_size = (int(size.width), int(size.height))
        self._last_frame = None

    def _sync_pool_to_window_size(self, hwnd: int) -> None:
        current_size = self._window_size(hwnd)
        if current_size != self._pool_size and all(current_size):
            self._recreate_pool(SizeInt32(*current_size))

    @staticmethod
    def _window_size(hwnd: int) -> tuple[int, int]:
        rect = _Rect()
        result = dwmapi.DwmGetWindowAttribute(
            hwnd,
            9,  # DWMWA_EXTENDED_FRAME_BOUNDS
            ctypes.byref(rect),
            ctypes.sizeof(rect),
        )
        if result < 0 and not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return (0, 0)
        return (int(rect.right - rect.left), int(rect.bottom - rect.top))

    @staticmethod
    def _validate_target(hwnd: int) -> None:
        if not user32.IsWindow(hwnd):
            raise ScreenTargetClosedError("選択したウィンドウは終了しました")
        if user32.IsIconic(hwnd):
            raise ScreenTargetPausedError("選択したウィンドウは最小化されています")
