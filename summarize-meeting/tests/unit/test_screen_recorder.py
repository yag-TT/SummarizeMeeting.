from __future__ import annotations

import threading
import time

import numpy as np

from summarize_meeting.capture.screen.base import ScreenTargetClosedError
from summarize_meeting.capture.screen.recorder import ScreenRecorder
from summarize_meeting.domain.capture import ScreenTarget
from summarize_meeting.domain.session import ComponentStatus
from summarize_meeting.infrastructure.screenshot_store import ScreenshotSaveError


class _ClosedBackend:
    def __init__(self) -> None:
        self.closed = False

    def list_targets(self):
        return []

    def capture(self, target):
        raise ScreenTargetClosedError(target.title)

    def close(self) -> None:
        self.closed = True


class _UnusedStore:
    count = 0

    def save(self, *args, **kwargs) -> None:
        raise AssertionError("closed target must not be stored")


class _DelayedBackend:
    def __init__(self) -> None:
        self.capture_started = threading.Event()
        self.release_capture = threading.Event()
        self.closed = False

    def list_targets(self):
        return []

    def capture(self, target):
        self.capture_started.set()
        assert self.release_capture.wait(1.0)
        return np.zeros((10, 10, 3), dtype=np.uint8)

    def close(self) -> None:
        self.closed = True


class _FrameBackend:
    def __init__(self) -> None:
        self.closed = False

    def list_targets(self):
        return []

    def capture(self, target):
        return np.zeros((10, 10, 3), dtype=np.uint8)

    def close(self) -> None:
        self.closed = True


class _RecoveringStore:
    def __init__(self, failures: int) -> None:
        self._remaining_failures = failures
        self.attempts = 0
        self.count = 0

    def save(self, *args, **kwargs) -> None:
        self.attempts += 1
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            raise ScreenshotSaveError("temporary failure")
        self.count += 1


def test_screen_recorder_closes_backend_after_target_closes() -> None:
    backend = _ClosedBackend()
    exceptions = []
    recorder = ScreenRecorder(
        backend=backend,
        target=ScreenTarget(id="1", title="closed"),
        store=_UnusedStore(),
        origin_ns=time.perf_counter_ns(),
        state_callback=lambda *args: None,
        count_callback=lambda count: None,
        evaluation_fps=100.0,
        exception_callback=lambda code, exc: exceptions.append((code, exc)),
    )

    recorder.start()
    deadline = time.monotonic() + 1.0
    while not backend.closed and time.monotonic() < deadline:
        time.sleep(0.01)
    recorder.finish()

    assert backend.closed
    assert exceptions[0][0] == "SCREEN_TARGET_CLOSED"
    assert isinstance(exceptions[0][1], ScreenTargetClosedError)


def test_screen_recorder_failure_stops_before_saving_new_frame() -> None:
    backend = _DelayedBackend()
    states = []
    recorder = ScreenRecorder(
        backend=backend,
        target=ScreenTarget(id="1", title="screen"),
        store=_UnusedStore(),
        origin_ns=time.perf_counter_ns(),
        state_callback=lambda status, code, message: states.append((status, code, message)),
        count_callback=lambda count: None,
        evaluation_fps=100.0,
    )

    recorder.start()
    assert backend.capture_started.wait(1.0)
    recorder.fail("LOW_DISK_SPACE", "screen storage stopped")
    backend.release_capture.set()
    recorder.finish()

    assert backend.closed
    assert any(code == "LOW_DISK_SPACE" for _status, code, _message in states)


def test_screen_recorder_retries_save_without_advancing_baseline() -> None:
    backend = _FrameBackend()
    store = _RecoveringStore(failures=1)
    states = []
    recorder = ScreenRecorder(
        backend=backend,
        target=ScreenTarget(id="1", title="screen"),
        store=store,
        origin_ns=time.perf_counter_ns(),
        state_callback=lambda status, code, message: states.append((status, code, message)),
        count_callback=lambda count: None,
        evaluation_fps=100.0,
    )

    recorder.start()
    deadline = time.monotonic() + 1.0
    while store.count == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    recorder.finish()

    assert store.attempts >= 2
    assert store.count == 1
    assert backend.closed
    assert any(code == "SCREEN_SAVE_FAILED" for _status, code, _message in states)
    assert any(message == "画面保存が復旧しました" for _status, _code, message in states)


def test_screen_recorder_continues_after_repeated_save_failure() -> None:
    backend = _FrameBackend()
    store = _RecoveringStore(failures=100)
    states = []
    exceptions = []
    recorder = ScreenRecorder(
        backend=backend,
        target=ScreenTarget(id="1", title="screen"),
        store=store,
        origin_ns=time.perf_counter_ns(),
        state_callback=lambda status, code, message: states.append((status, code, message)),
        count_callback=lambda count: None,
        evaluation_fps=100.0,
        exception_callback=lambda code, exc: exceptions.append((code, exc)),
    )

    recorder.start()
    deadline = time.monotonic() + 1.0
    while store.attempts < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    recorder.finish()

    assert store.attempts >= 3
    assert backend.closed
    assert sum(code == "SCREEN_SAVE_FAILED" for _status, code, _message in states) == 1
    assert [code for code, _exc in exceptions] == ["SCREEN_SAVE_FAILED"]
    assert all(status != ComponentStatus.FAILED for status, _code, _message in states)
