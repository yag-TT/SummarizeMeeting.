from __future__ import annotations

import threading
import time
from collections.abc import Callable

from summarize_meeting.capture.screen.base import (
    ScreenCaptureBackend,
    ScreenTargetClosedError,
    ScreenTargetPausedError,
)
from summarize_meeting.capture.screen.change_detector import ScreenChangeDetector
from summarize_meeting.domain.capture import ScreenTarget
from summarize_meeting.domain.session import ComponentStatus
from summarize_meeting.infrastructure.screenshot_store import ScreenshotStore

StateCallback = Callable[[ComponentStatus, str | None, str | None], None]
CountCallback = Callable[[int], None]


class ScreenRecorder:
    def __init__(
        self,
        *,
        backend: ScreenCaptureBackend,
        target: ScreenTarget,
        store: ScreenshotStore,
        origin_ns: int,
        state_callback: StateCallback,
        count_callback: CountCallback,
        evaluation_fps: float = 2.0,
    ) -> None:
        self._backend = backend
        self._target = target
        self._store = store
        self._origin_ns = origin_ns
        self._state_callback = state_callback
        self._count_callback = count_callback
        self._interval = 1.0 / evaluation_fps
        self._detector = ScreenChangeDetector()
        self._stop = threading.Event()
        self._failed = threading.Event()
        self._target_lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._state_callback(ComponentStatus.STARTING, None, None)
        self._thread = threading.Thread(target=self._run, name="screen-capture", daemon=True)
        self._thread.start()

    def replace_target(self, target: ScreenTarget) -> None:
        with self._target_lock:
            self._target = target
            self._detector.reset()
        if self._thread is None or not self._thread.is_alive():
            self._failed.clear()
            self._stop.clear()
            self.start()
        else:
            self._state_callback(ComponentStatus.STARTING, None, "画面を再選択しました")

    def request_stop(self) -> None:
        self._stop.set()

    def fail(self, error_code: str, message: str) -> None:
        if self._failed.is_set():
            return
        self._failed.set()
        self._state_callback(ComponentStatus.FAILED, error_code, message)
        self.request_stop()

    def finish(self, timeout: float = 5.0) -> None:
        if not self._failed.is_set():
            self._state_callback(ComponentStatus.STOPPING, None, None)
        self.request_stop()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        if self._thread is not None and self._thread.is_alive():
            raise TimeoutError("Screen capture did not stop")
        if not self._failed.is_set():
            self._state_callback(ComponentStatus.STOPPED, None, None)

    def _run(self) -> None:
        paused = False
        self._state_callback(ComponentStatus.RUNNING, None, None)
        try:
            while not self._stop.wait(self._interval):
                with self._target_lock:
                    target = self._target
                try:
                    frame = self._backend.capture(target)
                    if self._failed.is_set():
                        return
                    if paused:
                        paused = False
                        self._detector.reset()
                        self._state_callback(ComponentStatus.RUNNING, None, None)
                    timestamp_ms = (time.perf_counter_ns() - self._origin_ns) // 1_000_000
                    decision = self._detector.evaluate(frame, int(timestamp_ms))
                    if decision is None:
                        continue
                    self._store.save(
                        frame,
                        timestamp_ms=int(timestamp_ms),
                        reason=decision.reason,
                        metrics={
                            "changed_ratio": decision.metrics.changed_ratio,
                            "mean_abs_diff": decision.metrics.mean_abs_diff,
                        },
                    )
                    self._detector.mark_saved(decision)
                    self._count_callback(self._store.count)
                except ScreenTargetPausedError as exc:
                    if not paused:
                        paused = True
                        self._state_callback(
                            ComponentStatus.PAUSED,
                            "SCREEN_TARGET_PAUSED",
                            str(exc),
                        )
                except ScreenTargetClosedError as exc:
                    self._failed.set()
                    self._state_callback(
                        ComponentStatus.FAILED,
                        "SCREEN_TARGET_CLOSED",
                        str(exc),
                    )
                    return
                except Exception as exc:
                    self._failed.set()
                    self._state_callback(
                        ComponentStatus.FAILED,
                        "SCREEN_CAPTURE_FAILED",
                        str(exc),
                    )
                    return
        finally:
            self._backend.close()
