from __future__ import annotations

import ctypes
from collections.abc import Sequence
from ctypes import wintypes

import mss
import numpy as np

from summarize_meeting.capture.screen.base import (
    BgrFrame,
    ScreenTargetClosedError,
    ScreenTargetPausedError,
)
from summarize_meeting.domain.capture import ScreenTarget

user32 = ctypes.windll.user32


class Rect(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class WindowsMssScreenBackend:
    """Visible-window PoC adapter. It does not capture obscured window content."""

    def list_targets(self) -> Sequence[ScreenTarget]:
        targets: list[ScreenTarget] = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def callback(hwnd: int, _lparam: int) -> bool:
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            title = buffer.value.strip()
            if not title:
                return True
            rect = Rect()
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return True
            if rect.right <= rect.left or rect.bottom <= rect.top:
                return True
            targets.append(ScreenTarget(id=str(int(hwnd)), title=title))
            return True

        callback_ref = callback_type(callback)
        user32.EnumWindows(callback_ref, 0)
        return sorted(targets, key=lambda item: item.title.casefold())

    def capture(self, target: ScreenTarget) -> BgrFrame:
        hwnd = int(target.id)
        if not user32.IsWindow(hwnd):
            raise ScreenTargetClosedError("選択したウィンドウは終了しました")
        if user32.IsIconic(hwnd):
            raise ScreenTargetPausedError("選択したウィンドウは最小化されています")
        rect = Rect()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            raise ScreenTargetClosedError("選択したウィンドウの位置を取得できません")
        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
        if width <= 0 or height <= 0:
            raise ScreenTargetPausedError("選択したウィンドウの表示領域がありません")
        monitor = {
            "left": int(rect.left),
            "top": int(rect.top),
            "width": width,
            "height": height,
        }
        with mss.mss() as capture:
            bgra = np.asarray(capture.grab(monitor), dtype=np.uint8)
        return np.ascontiguousarray(bgra[:, :, :3])
