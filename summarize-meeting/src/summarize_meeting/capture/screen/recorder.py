"""取得画面の変化を監視し、意味のあるフレームだけを保存する。"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from contextlib import suppress

from summarize_meeting.capture.screen.base import (
    ScreenCaptureBackend,
    ScreenTargetClosedError,
    ScreenTargetPausedError,
)
from summarize_meeting.capture.screen.change_detector import ScreenChangeDetector
from summarize_meeting.domain.capture import ScreenTarget
from summarize_meeting.domain.session import ComponentStatus
from summarize_meeting.infrastructure.screenshot_store import (
    ScreenshotSaveError,
    ScreenshotStore,
)

StateCallback = Callable[[ComponentStatus, str | None, str | None], None]
CountCallback = Callable[[int], None]
ExceptionCallback = Callable[[str, Exception], None]


class ScreenRecorder:
    """画面フレームを定期評価し、変化検出時だけScreenshotStoreへ渡す。"""

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
        detector: ScreenChangeDetector | None = None,
        exception_callback: ExceptionCallback | None = None,
    ) -> None:
        self._backend = backend
        self._target = target
        self._store = store
        self._origin_ns = origin_ns
        self._state_callback = state_callback
        self._count_callback = count_callback
        self._exception_callback = exception_callback
        self._interval = 1.0 / evaluation_fps
        self._detector = detector or ScreenChangeDetector()
        self._stop = threading.Event()
        self._failed = threading.Event()
        self._target_lock = threading.Lock()
        self._awaiting_first_frame = threading.Event()
        self._awaiting_first_frame.set()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._state_callback(ComponentStatus.STARTING, None, None)
        self._thread = threading.Thread(target=self._run, name="screen-capture", daemon=True)
        self._thread.start()

    def replace_target(self, target: ScreenTarget) -> None:
        with self._target_lock:
            self._target = target
            # 新しい対象の最初のフレームは旧対象との差分として扱わない。
            self._detector.reset()
            self._awaiting_first_frame.set()
        thread_is_alive = self._thread is not None and self._thread.is_alive()
        if thread_is_alive:
            self._backend.replace_target(target)
            self._state_callback(ComponentStatus.STARTING, None, "画面を再選択しました")
        else:
            self._failed.clear()
            self._stop.clear()
            self.start()

    def request_stop(self) -> None:
        self._stop.set()
        with suppress(Exception):
            self._backend.stop()

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
        save_failed = False
        try:
            with self._target_lock:
                initial_target = self._target
            self._backend.start(initial_target)
            self._state_callback(ComponentStatus.RUNNING, None, None)
            while not self._stop.wait(self._interval):
                try:
                    frame = self._backend.read_latest_frame(
                        120.0
                        if self._awaiting_first_frame.is_set()
                        else max(2.0, self._interval * 4)
                    )
                    self._awaiting_first_frame.clear()
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
                    # 保存に成功した場合だけ基準画像を更新し、失敗時は次回に再試行する。
                    try:
                        self._store.save(
                            frame,
                            timestamp_ms=int(timestamp_ms),
                            reason=decision.reason,
                            metrics={
                                "changed_ratio": decision.metrics.changed_ratio,
                                "mean_abs_diff": decision.metrics.mean_abs_diff,
                            },
                        )
                    except ScreenshotSaveError as exc:
                        if not save_failed:
                            save_failed = True
                            self._notify_exception("SCREEN_SAVE_FAILED", exc)
                            self._state_callback(
                                ComponentStatus.RUNNING,
                                "SCREEN_SAVE_FAILED",
                                str(exc),
                            )
                        continue
                    if save_failed:
                        save_failed = False
                        self._state_callback(
                            ComponentStatus.RUNNING,
                            None,
                            "画面保存が復旧しました",
                        )
                    self._detector.mark_saved(decision)
                    self._count_callback(self._store.count)
                except ScreenTargetPausedError as exc:
                    if self._stop.is_set():
                        return
                    if not paused:
                        paused = True
                        self._notify_exception("SCREEN_TARGET_PAUSED", exc)
                        self._state_callback(
                            ComponentStatus.PAUSED,
                            "SCREEN_TARGET_PAUSED",
                            str(exc),
                        )
                except ScreenTargetClosedError as exc:
                    if self._stop.is_set():
                        return
                    self._notify_exception("SCREEN_TARGET_CLOSED", exc)
                    self._failed.set()
                    self._state_callback(
                        ComponentStatus.FAILED,
                        "SCREEN_TARGET_CLOSED",
                        str(exc),
                    )
                    return
                except Exception as exc:
                    if self._stop.is_set():
                        return
                    self._notify_exception("SCREEN_CAPTURE_FAILED", exc)
                    self._failed.set()
                    self._state_callback(
                        ComponentStatus.FAILED,
                        "SCREEN_CAPTURE_FAILED",
                        str(exc),
                    )
                    return
        except ScreenTargetClosedError as exc:
            if self._stop.is_set():
                return
            self._notify_exception("SCREEN_TARGET_CLOSED", exc)
            self._failed.set()
            self._state_callback(
                ComponentStatus.FAILED,
                "SCREEN_TARGET_CLOSED",
                str(exc),
            )
        except Exception as exc:
            if self._stop.is_set():
                return
            self._notify_exception("SCREEN_CAPTURE_FAILED", exc)
            self._failed.set()
            self._state_callback(
                ComponentStatus.FAILED,
                "SCREEN_CAPTURE_FAILED",
                str(exc),
            )
        finally:
            try:
                self._backend.stop()
            except Exception as exc:
                self._notify_exception("SCREEN_CLOSE_FAILED", exc)
                if not self._failed.is_set():
                    self._failed.set()
                    self._state_callback(
                        ComponentStatus.FAILED,
                        "SCREEN_CLOSE_FAILED",
                        str(exc),
                    )

    def _notify_exception(self, error_code: str, exception: Exception) -> None:
        if self._exception_callback is None:
            return
        with suppress(Exception):
            self._exception_callback(error_code, exception)
