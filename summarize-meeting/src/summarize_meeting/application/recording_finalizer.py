"""録音取得元を停止し、成果物と最終セッション状態を確定する。"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping

from summarize_meeting.application.storage_monitor import StorageMonitor
from summarize_meeting.capture.audio.recorder import AudioTrackRecorder
from summarize_meeting.capture.screen.recorder import ScreenRecorder
from summarize_meeting.domain.session import (
    ComponentKind,
    ComponentStatus,
    RecordingSession,
    SessionStatus,
)
from summarize_meeting.infrastructure.audio_writer import AudioTrackStats
from summarize_meeting.infrastructure.session_repository import SessionPaths

ProgressCallback = Callable[[int, str], None]


class RecordingFinalizer:
    """停止要求からmanifest・session.json確定までを順序付きで実行する。"""

    def __init__(
        self,
        *,
        session: RecordingSession,
        paths: SessionPaths,
        audio_recorders: Mapping[ComponentKind, AudioTrackRecorder],
        screen_recorder: ScreenRecorder | None,
        storage_monitor: StorageMonitor,
        origin_ns: int,
        screenshot_count: int,
        state_lock: threading.RLock,
        emit_progress: ProgressCallback,
        append_event: Callable[..., None],
        save_session: Callable[[SessionPaths, RecordingSession, str], bool],
        log_exception: Callable[[str, BaseException, str, str], None],
        set_component: Callable[[ComponentKind, ComponentStatus, str | None, str | None], None],
        set_track_ranges: Callable[[dict[ComponentKind, tuple[int, int]]], None],
        write_audio_manifest: Callable[..., None],
        close_session_log: Callable[[], None],
        notify_fatal: Callable[[str], None],
        notify_finished: Callable[[str], None],
        terminal_event: threading.Event,
    ) -> None:
        self._session = session
        self._paths = paths
        self._audio_recorders = dict(audio_recorders)
        self._screen_recorder = screen_recorder
        self._storage_monitor = storage_monitor
        self._origin_ns = origin_ns
        self._screenshot_count = screenshot_count
        self._state_lock = state_lock
        self._emit_progress = emit_progress
        self._append_event = append_event
        self._save_session = save_session
        self._log_exception = log_exception
        self._set_component = set_component
        self._set_track_ranges = set_track_ranges
        self._write_audio_manifest = write_audio_manifest
        self._close_session_log = close_session_log
        self._notify_fatal = notify_fatal
        self._notify_finished = notify_finished
        self._terminal_event = terminal_event

    def run(self) -> None:
        try:
            self._run()
        except Exception as exc:
            logging.getLogger(__name__).exception("Unexpected recording finalization failure")
            with self._state_lock:
                self._session.ended_at = RecordingSession.now_iso()
                self._session.duration_ms = (
                    time.perf_counter_ns() - self._origin_ns
                ) // 1_000_000
                self._session.status = SessionStatus.INTERRUPTED
                self._session.add_warning(
                    "FINALIZE_FAILED",
                    f"unexpected finalizer error: {exc}",
                    self._session.duration_ms,
                )
                self._save_session(
                    self._paths,
                    self._session,
                    "unexpected_finalize_failure",
                )
            self._notify_fatal(f"記録の確定中に予期しないエラーが発生しました: {exc}")
            self._emit_progress(100, "エラーを含む記録を保存しました")
        finally:
            self._close_session_log()
            self._terminal_event.set()
            self._notify_finished(str(self._paths.root))

    def _run(self) -> None:
        with self._state_lock:
            self._session.status = SessionStatus.STOPPING
            self._save_session(self._paths, self._session, "session_stopping")
            audio_kinds = list(self._audio_recorders)
            ranges = {
                kind: (
                    15 + (70 * index // max(1, len(audio_kinds))),
                    15 + (70 * (index + 1) // max(1, len(audio_kinds))),
                )
                for index, kind in enumerate(audio_kinds)
            }
            self._set_track_ranges(ranges)
        self._emit_progress(0, "記録の停止を開始しています")
        self._append_event("session_stopping")
        self._storage_monitor.request_stop()

        for recorder in self._audio_recorders.values():
            recorder.request_stop()
        if self._screen_recorder is not None:
            self._screen_recorder.request_stop()
        self._emit_progress(8, "画面取得を停止しています")

        stats: dict[str, AudioTrackStats] = {}
        failures: list[str] = []
        self._finish_screen(failures)
        self._emit_progress(12, "画面取得を停止しました")

        with self._state_lock:
            self._session.status = SessionStatus.FINALIZING
            self._save_session(self._paths, self._session, "session_finalizing")
        self._emit_progress(15, "音声ファイルを確定しています")
        self._finish_audio(stats, failures, ranges)
        self._emit_progress(85, "保存状態を確認しています")
        self._finish_storage_monitor(failures)
        self._emit_progress(90, "音声情報を保存しています")

        with self._state_lock:
            storage_failed = (
                self._session.components[ComponentKind.SESSION_STORAGE.value].status
                == ComponentStatus.FAILED
            )
        if not storage_failed:
            self._set_component(
                ComponentKind.SESSION_STORAGE,
                ComponentStatus.STOPPED,
                None,
                None,
            )
        try:
            self._write_audio_manifest(
                self._paths.audio / "manifest.json",
                stats,
                monotonic_origin_ns=self._origin_ns,
            )
        except OSError as exc:
            failures.append(f"audio manifest: {exc}")
            self._record_exception(exc, ComponentKind.SESSION_STORAGE)

        self._emit_progress(95, "セッション情報を保存しています")
        cleanup_warnings = [
            f"{track}: {track_stats.work_cleanup_error}"
            for track, track_stats in stats.items()
            if track_stats.work_cleanup_error is not None
        ]
        for warning in cleanup_warnings:
            self._append_event("audio_work_cleanup_failed", message=warning)
        self._complete_session(stats, failures, cleanup_warnings)

    def _finish_screen(self, failures: list[str]) -> None:
        if self._screen_recorder is None:
            return
        try:
            self._screen_recorder.finish()
        except Exception as exc:
            failures.append(f"screen: {exc}")
            self._record_exception(exc, ComponentKind.SCREEN)

    def _finish_audio(
        self,
        stats: dict[str, AudioTrackStats],
        failures: list[str],
        ranges: Mapping[ComponentKind, tuple[int, int]],
    ) -> None:
        for kind, recorder in self._audio_recorders.items():
            start, end = ranges[kind]
            self._emit_progress(start, f"{_audio_label(kind)}を停止しています")
            try:
                result = recorder.finish()
                if result is not None:
                    stats[kind.value] = result
            except Exception as exc:
                failures.append(f"{kind.value}: {exc}")
                self._record_exception(exc, kind)
                self._set_component(
                    kind,
                    ComponentStatus.FAILED,
                    "FINALIZE_FAILED",
                    str(exc),
                )
            self._emit_progress(end, f"{_audio_label(kind)}を確定しました")

    def _finish_storage_monitor(self, failures: list[str]) -> None:
        try:
            self._storage_monitor.finish()
        except Exception as exc:
            failures.append(f"storage monitor: {exc}")
            self._record_exception(exc, ComponentKind.SESSION_STORAGE)

    def _complete_session(
        self,
        stats: Mapping[str, AudioTrackStats],
        failures: list[str],
        cleanup_warnings: list[str],
    ) -> None:
        ended_ns = time.perf_counter_ns()
        with self._state_lock:
            self._session.ended_at = RecordingSession.now_iso()
            self._session.duration_ms = (ended_ns - self._origin_ns) // 1_000_000
            self._session.status = (
                SessionStatus.INTERRUPTED if failures else SessionStatus.RECORDED
            )
            for failure in failures:
                self._session.add_warning(
                    "FINALIZE_FAILED", failure, self._session.duration_ms
                )
            for warning in cleanup_warnings:
                self._session.add_warning(
                    "AUDIO_WORK_CLEANUP_FAILED",
                    warning,
                    self._session.duration_ms,
                )
            metadata_saved = self._save_session(
                self._paths,
                self._session,
                "session_finished",
            )
            if not metadata_saved:
                failure = "session metadata: final session.json write failed"
                failures.append(failure)
                self._session.status = SessionStatus.INTERRUPTED
                self._session.add_warning(
                    "FINALIZE_FAILED", failure, self._session.duration_ms
                )
        self._emit_progress(99, "保存結果を確認しています")
        audio_summary = {
            track: {
                "frames": value.frames_written,
                "segments": value.segments,
                "duration_ms": value.audio_duration_ms,
                "validated": value.validated,
                "overflow_count": value.overflow_count,
                "queue_pressure_count": value.queue_pressure_count,
            }
            for track, value in stats.items()
        }
        self._append_event(
            "session_finished",
            status=self._session.status.value,
            duration_ms=self._session.duration_ms,
            screenshot_count=self._screenshot_count,
            audio_summary=audio_summary,
            failure_count=len(failures),
            failures=failures,
        )
        if failures:
            self._notify_fatal(
                "一部の記録を正常に確定できませんでした: " + "; ".join(failures)
            )
        self._emit_progress(
            100,
            "エラーを含む記録を保存しました" if failures else "記録を保存しました",
        )
    def _record_exception(self, error: BaseException, component: ComponentKind) -> None:
        self._log_exception(
            "finalize_failed",
            error,
            component.value,
            "FINALIZE_FAILED",
        )


def _audio_label(kind: ComponentKind) -> str:
    return "マイク音声" if kind == ComponentKind.MICROPHONE else "PC音声"
