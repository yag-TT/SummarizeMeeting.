from __future__ import annotations

import os
import threading
import time

import pytest
from PySide6.QtWidgets import QLabel

from summarize_meeting.capture.screen.qt_capture import (
    QtScreenCaptureBackend,
    _is_wayland,
)


@pytest.mark.skipif(
    os.environ.get("SUMMARIZE_MEETING_RUN_QT_CAPTURE_TESTS") != "1",
    reason="interactive Qt capture test is disabled",
)
def test_qt_capture_reads_own_window(qapp) -> None:
    if _is_wayland():
        pytest.skip("Wayland requires an interactive Portal selection")
    title = f"Summarize Meeting Qt Capture Test {os.getpid()}"
    window = QLabel("Qt capture test")
    window.setWindowTitle(title)
    window.resize(640, 360)
    window.show()
    window.raise_()
    window.activateWindow()
    qapp.processEvents()
    backend = QtScreenCaptureBackend(frame_interval_seconds=0.01)
    try:
        target = next(item for item in backend.list_targets() if item.title == title)
        backend.start(target)
        result: list[object] = []

        def read_frame() -> None:
            try:
                result.append(backend.read_latest_frame(20.0))
            except Exception as exc:
                result.append(exc)

        reader = threading.Thread(target=read_frame, daemon=True)
        reader.start()
        deadline = time.monotonic() + 20.0
        repaint_count = 0
        while reader.is_alive() and time.monotonic() < deadline:
            repaint_count += 1
            window.setText(f"Qt capture test {repaint_count}")
            window.repaint()
            qapp.processEvents()
            time.sleep(0.05)
        reader.join(timeout=0.5)

        assert result and not isinstance(result[0], Exception), result
        frame = result[0]
        shape = getattr(frame, "shape", ())
        assert len(shape) == 3 and shape[2] == 3
        assert shape[0] >= 360 and shape[1] >= 640
    finally:
        backend.stop()
        window.close()
