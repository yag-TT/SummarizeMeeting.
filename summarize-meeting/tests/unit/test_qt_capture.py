from __future__ import annotations

import numpy as np
import pytest
from PySide6.QtGui import QImage
from PySide6.QtMultimedia import QVideoFrame

from summarize_meeting.capture.screen.base import (
    ScreenTargetClosedError,
    ScreenTargetPausedError,
)
from summarize_meeting.capture.screen.qt_capture import (
    QtScreenCaptureBackend,
    video_frame_to_bgr,
)


def test_video_frame_to_bgr_owns_converted_pixels() -> None:
    pixels = np.array([[[30, 20, 10, 255], [60, 50, 40, 255]]], dtype=np.uint8)
    image = QImage(
        pixels.data,
        2,
        1,
        pixels.strides[0],
        QImage.Format.Format_RGBA8888,
    )

    result = video_frame_to_bgr(QVideoFrame(image))

    assert result.tolist() == [[[10, 20, 30], [40, 50, 60]]]
    pixels[0, 0, 0] = 99
    assert result[0, 0, 2] == 30
    assert result.flags["C_CONTIGUOUS"]


def test_wayland_lists_portal_target(qapp, monkeypatch) -> None:
    backend = QtScreenCaptureBackend()
    monkeypatch.setattr(backend, "_ensure_objects", lambda: None)
    monkeypatch.setattr("summarize_meeting.capture.screen.qt_capture._is_wayland", lambda: True)

    targets = backend.list_targets()

    assert [(target.id, target.kind) for target in targets] == [("qt-portal", "portal")]
    assert "OSダイアログ" in targets[0].title


def test_capture_error_wakes_waiting_reader(qapp) -> None:
    backend = QtScreenCaptureBackend()
    backend._on_capture_error(object(), "permission denied")  # noqa: SLF001

    with pytest.raises(ScreenTargetClosedError, match="permission denied"):
        backend.read_latest_frame(0.1)


def test_latest_frame_is_returned_only_once(qapp) -> None:
    backend = QtScreenCaptureBackend(frame_interval_seconds=0.001)
    pixels = np.array([[[30, 20, 10, 255]]], dtype=np.uint8)
    image = QImage(
        pixels.data,
        1,
        1,
        pixels.strides[0],
        QImage.Format.Format_RGBA8888,
    )
    backend._on_video_frame(QVideoFrame(image))  # noqa: SLF001

    assert backend.read_latest_frame(0.1).tolist() == [[[10, 20, 30]]]
    with pytest.raises(ScreenTargetPausedError, match="新しいフレーム"):
        backend.read_latest_frame(0.01)


def test_video_frame_conversion_handles_padded_stride() -> None:
    pixels = np.array(
        [
            [30, 20, 10, 255, 0, 0, 0, 0],
            [60, 50, 40, 255, 0, 0, 0, 0],
        ],
        dtype=np.uint8,
    )
    image = QImage(
        pixels.data,
        1,
        2,
        pixels.strides[0],
        QImage.Format.Format_RGBA8888,
    )

    result = video_frame_to_bgr(QVideoFrame(image))

    assert result.tolist() == [[[10, 20, 30]], [[40, 50, 60]]]


class _Capture:
    def __init__(self) -> None:
        self.stops = 0

    def stop(self) -> None:
        self.stops += 1


def test_stop_stops_both_capture_sources(qapp) -> None:
    backend = QtScreenCaptureBackend()
    screen = _Capture()
    window = _Capture()
    backend._screen_capture = screen  # type: ignore[assignment]  # noqa: SLF001
    backend._window_capture = window  # type: ignore[assignment]  # noqa: SLF001

    backend.stop()

    assert screen.stops == 1
    assert window.stops == 1
