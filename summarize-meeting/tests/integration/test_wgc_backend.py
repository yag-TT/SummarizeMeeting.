from __future__ import annotations

import os
import time
from concurrent.futures import Future, ThreadPoolExecutor

import pytest
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QWidget

from summarize_meeting.capture.screen.windows_wgc import WindowsWgcScreenBackend
from summarize_meeting.domain.capture import ScreenTarget

pytestmark = pytest.mark.skipif(
    os.environ.get("SUMMARIZE_MEETING_RUN_WGC_TESTS") != "1",
    reason="set SUMMARIZE_MEETING_RUN_WGC_TESTS=1 on an interactive Windows desktop",
)


def _wait_for_capture(app: QApplication, future: Future, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while not future.done() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    return future.result(timeout=0.1)


def test_wgc_captures_and_resizes_own_window(qapp: QApplication) -> None:
    window = QWidget()
    window.setWindowTitle("Summarize Meeting WGC integration test")
    window.resize(320, 180)
    palette = window.palette()
    palette.setColor(QPalette.ColorRole.Window, QColor(20, 150, 220))
    window.setPalette(palette)
    window.setAutoFillBackground(True)
    window.show()
    qapp.processEvents()

    backend = WindowsWgcScreenBackend()
    target = ScreenTarget(id=str(int(window.winId())), title=window.windowTitle())
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = _wait_for_capture(qapp, pool.submit(backend.capture, target))
            second = _wait_for_capture(qapp, pool.submit(backend.capture, target))
            window.resize(400, 240)
            resized = _wait_for_capture(qapp, pool.submit(backend.capture, target))

        center = resized[resized.shape[0] // 2, resized.shape[1] // 2]
        assert first.shape == second.shape
        assert resized.shape[0] > first.shape[0]
        assert resized.shape[1] > first.shape[1]
        assert center.tolist() == [220, 150, 20]
    finally:
        backend.close()
        window.close()
