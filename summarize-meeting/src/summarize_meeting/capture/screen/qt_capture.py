from __future__ import annotations

import hashlib
import os
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot
from PySide6.QtGui import QGuiApplication, QImage
from PySide6.QtMultimedia import (
    QCapturableWindow,
    QMediaCaptureSession,
    QScreenCapture,
    QVideoFrame,
    QVideoSink,
    QWindowCapture,
)

from summarize_meeting.capture.screen.base import (
    BgrFrame,
    ScreenTargetClosedError,
    ScreenTargetPausedError,
)
from summarize_meeting.domain.capture import ScreenTarget

_PORTAL_TARGET = ScreenTarget(
    id="qt-portal",
    title="開始時にOSダイアログで共有画面を選択",
    kind="portal",
)


@dataclass(slots=True)
class _Request:
    operation: Callable[..., Any]
    arguments: tuple[Any, ...]
    completed: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: BaseException | None = None


class QtScreenCaptureBackend(QObject):
    """Qt Multimedia screen capture shared by Windows, X11, and Wayland."""

    _request_queued = Signal(object)

    def __init__(self, *, frame_interval_seconds: float = 0.45) -> None:
        super().__init__()
        if frame_interval_seconds <= 0:
            raise ValueError("frame_interval_seconds must be positive")
        self._frame_interval_seconds = frame_interval_seconds
        self._request_queued.connect(
            self._execute_request,
            Qt.ConnectionType.QueuedConnection,
        )
        self._session: QMediaCaptureSession | None = None
        self._screen_capture: QScreenCapture | None = None
        self._window_capture: QWindowCapture | None = None
        self._sink: QVideoSink | None = None
        self._sources: dict[str, object] = {}
        self._condition = threading.Condition()
        self._latest_frame: BgrFrame | None = None
        self._frame_version = 0
        self._last_read_version = 0
        self._last_frame_time = 0.0
        self._capture_error: str | None = None

    def list_targets(self) -> Sequence[ScreenTarget]:
        return self._invoke(self._list_targets_on_qt_thread, timeout=5.0)

    def start(self, target: ScreenTarget) -> None:
        self._invoke(self._start_on_qt_thread, target, timeout=5.0)

    def read_latest_frame(self, timeout: float) -> BgrFrame:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._frame_version <= self._last_read_version:
                if self._capture_error is not None:
                    raise ScreenTargetClosedError(self._capture_error)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ScreenTargetPausedError(
                        "共有画面の新しいフレームを取得できませんでした"
                    )
                self._condition.wait(remaining)
            assert self._latest_frame is not None
            self._last_read_version = self._frame_version
            return self._latest_frame.copy()

    def replace_target(self, target: ScreenTarget) -> None:
        self.start(target)

    def stop(self) -> None:
        with self._condition:
            self._capture_error = "画面取得を停止しました"
            self._condition.notify_all()
        if QThread.currentThread() == self.thread():
            self._stop_on_qt_thread()
            return
        self._request_queued.emit(_Request(self._stop_on_qt_thread, ()))

    def close(self) -> None:
        self.stop()

    def _invoke(self, operation: Callable[..., Any], *arguments: Any, timeout: float) -> Any:
        if QThread.currentThread() == self.thread():
            return operation(*arguments)
        request = _Request(operation, arguments)
        self._request_queued.emit(request)
        if not request.completed.wait(timeout):
            raise RuntimeError("Qt画面取得サービスが応答しません")
        if request.error is not None:
            raise request.error
        return request.result

    @Slot(object)
    def _execute_request(self, value: object) -> None:
        if not isinstance(value, _Request):
            return
        try:
            value.result = value.operation(*value.arguments)
        except BaseException as exc:
            value.error = exc
        finally:
            value.completed.set()

    def _ensure_objects(self) -> None:
        if self._session is not None:
            return
        if QGuiApplication.instance() is None:
            raise RuntimeError("Qt画面取得にはQGuiApplicationが必要です")
        self._session = QMediaCaptureSession(self)
        self._screen_capture = QScreenCapture(self)
        self._window_capture = QWindowCapture(self)
        self._sink = QVideoSink(self)
        self._session.setScreenCapture(self._screen_capture)
        self._session.setWindowCapture(self._window_capture)
        self._session.setVideoSink(self._sink)
        self._screen_capture.errorOccurred.connect(self._on_capture_error)
        self._window_capture.errorOccurred.connect(self._on_capture_error)
        self._sink.videoFrameChanged.connect(self._on_video_frame)

    def _list_targets_on_qt_thread(self) -> list[ScreenTarget]:
        self._ensure_objects()
        if _is_wayland():
            self._sources = {_PORTAL_TARGET.id: _PORTAL_TARGET}
            return [_PORTAL_TARGET]

        targets: list[ScreenTarget] = []
        sources: dict[str, object] = {}
        for index, screen in enumerate(QGuiApplication.screens()):
            title = screen.name().strip() or f"画面 {index + 1}"
            target = ScreenTarget(
                id=_target_id("screen", title, index),
                title=f"画面: {title}",
                kind="screen",
            )
            targets.append(target)
            sources[target.id] = screen
        assert self._window_capture is not None
        for index, window in enumerate(self._window_capture.capturableWindows()):
            title = window.description().strip()
            if not title or not window.isValid():
                continue
            target = ScreenTarget(
                id=_target_id("window", title, index),
                title=title,
                kind="window",
            )
            targets.append(target)
            sources[target.id] = window
        self._sources = sources
        return targets

    def _start_on_qt_thread(self, target: ScreenTarget) -> None:
        self._ensure_objects()
        self._stop_on_qt_thread()
        with self._condition:
            self._latest_frame = None
            self._frame_version = 0
            self._last_read_version = 0
            self._capture_error = None
            self._last_frame_time = 0.0

        if target.kind == "portal":
            if not _is_wayland():
                raise ScreenTargetClosedError("Portal画面選択はWayland専用です")
            assert self._screen_capture is not None
            self._screen_capture.start()
            return

        source = self._sources.get(target.id)
        if source is None:
            self._list_targets_on_qt_thread()
            source = self._sources.get(target.id)
        if source is None:
            raise ScreenTargetClosedError("選択した共有対象が見つかりません")
        if target.kind == "window":
            if not isinstance(source, QCapturableWindow) or not source.isValid():
                raise ScreenTargetClosedError("選択したウィンドウは終了しました")
            assert self._window_capture is not None
            self._window_capture.setWindow(source)
            self._window_capture.start()
        elif target.kind == "screen":
            assert self._screen_capture is not None
            self._screen_capture.setScreen(source)
            self._screen_capture.start()
        else:
            raise ScreenTargetClosedError(f"未対応の共有対象です: {target.kind}")

    def _stop_on_qt_thread(self) -> None:
        if self._screen_capture is not None:
            self._screen_capture.stop()
        if self._window_capture is not None:
            self._window_capture.stop()

    @Slot(object, str)
    def _on_capture_error(self, _error: object, message: str) -> None:
        detail = message.strip() or "共有画面の取得が拒否されたか失敗しました"
        with self._condition:
            self._capture_error = detail
            self._condition.notify_all()

    @Slot(QVideoFrame)
    def _on_video_frame(self, frame: QVideoFrame) -> None:
        now = time.monotonic()
        if now - self._last_frame_time < self._frame_interval_seconds:
            return
        try:
            converted = video_frame_to_bgr(frame)
        except Exception as exc:
            with self._condition:
                self._capture_error = f"共有画面の画像変換に失敗しました: {exc}"
                self._condition.notify_all()
            return
        self._last_frame_time = now
        with self._condition:
            self._latest_frame = converted
            self._frame_version += 1
            self._condition.notify_all()


def video_frame_to_bgr(frame: QVideoFrame) -> BgrFrame:
    if not frame.isValid():
        raise ValueError("invalid QVideoFrame")
    image = frame.toImage()
    if image.isNull():
        raise ValueError("QVideoFrame cannot be converted to QImage")
    rgba = image.convertedTo(QImage.Format.Format_RGBA8888)
    width = rgba.width()
    height = rgba.height()
    bytes_per_line = rgba.bytesPerLine()
    buffer = np.frombuffer(rgba.bits(), dtype=np.uint8, count=rgba.sizeInBytes())
    rows = buffer.reshape(height, bytes_per_line)
    pixels = rows[:, : width * 4].reshape(height, width, 4)
    return np.ascontiguousarray(pixels[:, :, 2::-1])


def _is_wayland() -> bool:
    application = QGuiApplication.instance()
    platform_name = application.platformName().casefold() if application is not None else ""
    session_type = os.environ.get("XDG_SESSION_TYPE", "").casefold()
    return "wayland" in platform_name or session_type == "wayland"


def _target_id(kind: str, title: str, index: int) -> str:
    digest = hashlib.sha256(f"{kind}\0{title}\0{index}".encode()).hexdigest()[:20]
    return f"qt-{kind}:{digest}"
