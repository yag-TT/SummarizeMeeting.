from __future__ import annotations

from summarize_meeting.capture.screen.base import ScreenCaptureBackend
from summarize_meeting.capture.screen.qt_capture import QtScreenCaptureBackend


def create_screen_capture_backend() -> ScreenCaptureBackend:
    return QtScreenCaptureBackend()
