from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from summarize_meeting.application.storage_monitor import (
    StorageCapacity,
    StorageCapacityCheckError,
    StorageMonitor,
    format_gib,
)
from summarize_meeting.capture.audio.recorder import AudioTrackRecorder
from summarize_meeting.capture.audio.soundcard_backend import SoundCardAudioBackend
from summarize_meeting.capture.screen.change_detector import ScreenChangeDetector
from summarize_meeting.capture.screen.platform_backend import create_screen_capture_backend
from summarize_meeting.capture.screen.recorder import ScreenRecorder
from summarize_meeting.domain.capture import AudioDevice, ScreenTarget
from summarize_meeting.domain.session import (
    AUDIO_MANIFEST_SCHEMA_VERSION,
    ComponentKind,
    ComponentStatus,
    RecordingSession,
    SessionStatus,
)
from summarize_meeting.infrastructure.audio_writer import AudioTrackStats
from summarize_meeting.infrastructure.paths import PortableAppPaths
from summarize_meeting.infrastructure.screenshot_store import ScreenshotStore
from summarize_meeting.infrastructure.session_log import SessionLogWriter
from summarize_meeting.infrastructure.session_repository import (
    FileSessionRepository,
    SessionPaths,
)
from summarize_meeting.infrastructure.settings import AppSettings, FileSettingsRepository
from summarize_meeting.infrastructure.storage_probe import SystemStorageProbe


@dataclass(frozen=True, slots=True)
class CaptureSourcesSnapshot:
    microphones: tuple[AudioDevice, ...]
    system_audio: tuple[AudioDevice, ...]
    screens: tuple[ScreenTarget, ...]
    errors: tuple[str, ...] = ()


class RecordingController(QObject):
    component_changed = Signal(str, str, str)
    meter_changed = Signal(str, float)
    screenshot_count_changed = Signal(int)
    sources_refreshed = Signal(int, object)
    session_preparing = Signal(str)
    session_started = Signal(str)
    session_start_failed = Signal(str, str)
    session_start_cancelled = Signal(str)
    finalize_progress = Signal(int, str)
    session_finished = Signal(str)
    fatal_error = Signal(str)

    def __init__(
        self,
        app_paths: PortableAppPaths,
        *,
        storage_monitor: StorageMonitor | None = None,
        settings: AppSettings | None = None,
        settings_repository: FileSettingsRepository | None = None,
        audio_start_timeout_seconds: float = 5.0,
    ) -> None:
        super().__init__()
        self._app_paths = app_paths
        self._repository = FileSessionRepository(app_paths.meetings_dir)
        self._audio_backend = SoundCardAudioBackend()
        self._screen_backend = create_screen_capture_backend()
        self._settings_repository = settings_repository or FileSettingsRepository(
            app_paths.settings_file
        )
        self._settings = settings or self._settings_repository.load().settings
        self._storage_monitor = storage_monitor or StorageMonitor(
            path=app_paths.meetings_dir,
            probe=SystemStorageProbe(),
        )
        if audio_start_timeout_seconds <= 0:
            raise ValueError("audio_start_timeout_seconds must be positive")
        self._audio_start_timeout_seconds = audio_start_timeout_seconds
        self._session: RecordingSession | None = None
        self._session_paths: SessionPaths | None = None
        self._session_log: SessionLogWriter | None = None
        self._origin_ns = 0
        self._audio_recorders: dict[ComponentKind, AudioTrackRecorder] = {}
        self._screen_recorder: ScreenRecorder | None = None
        self._lock = threading.RLock()
        self._start_thread: threading.Thread | None = None
        self._startup_cancel = threading.Event()
        self._stop_thread: threading.Thread | None = None
        self._audio_unavailable_notified = False
        self._metadata_write_failed_notified = False
        self._screen_disabled_by_storage = False
        self._screenshot_count = 0
        self._finalize_progress_percent = -1
        self._finalize_progress_message = ""
        self._finalize_track_ranges: dict[ComponentKind, tuple[int, int]] = {}
        self._session_terminal = threading.Event()
        self._session_terminal.set()

    @property
    def last_microphone_device_id(self) -> str | None:
        return self._settings.last_microphone_device_id

    @property
    def last_system_device_id(self) -> str | None:
        return self._settings.last_system_device_id

    @property
    def meetings_directory(self) -> Path:
        return self._app_paths.meetings_dir

    @property
    def auto_transcribe_after_recording(self) -> bool:
        return self._settings.auto_transcribe_after_recording

    def set_auto_transcribe_after_recording(self, enabled: bool) -> None:
        updated = replace(self._settings, auto_transcribe_after_recording=bool(enabled))
        try:
            self._settings_repository.save(updated)
        except OSError as exc:
            raise RuntimeError(f"自動文字起こし設定を保存できません: {exc}") from exc
        self._settings = updated

    @property
    def is_recording(self) -> bool:
        with self._lock:
            return self._session is not None and self._session.status in {
                SessionStatus.PREPARING,
                SessionStatus.RECORDING,
                SessionStatus.STOPPING,
                SessionStatus.FINALIZING,
            }

    def list_input_devices(self) -> list[AudioDevice]:
        return list(self._audio_backend.list_input_devices())

    def list_loopback_devices(self) -> list[AudioDevice]:
        return list(self._audio_backend.list_loopback_devices())

    def list_screen_targets(self) -> list[ScreenTarget]:
        return list(self._screen_backend.list_targets())

    def refresh_sources_async(self, request_id: int) -> None:
        thread = threading.Thread(
            target=self._refresh_sources_worker,
            args=(request_id,),
            name=f"source-refresh-{request_id}",
            daemon=True,
        )
        thread.start()

    def _refresh_sources_worker(self, request_id: int) -> None:
        errors: list[str] = []
        try:
            microphones = tuple(self.list_input_devices())
        except Exception as exc:
            microphones = ()
            errors.append(f"マイク一覧を取得できません: {exc}")
            logging.getLogger(__name__).warning(
                "Microphone enumeration failed",
                exc_info=exc,
            )
        try:
            system_audio = tuple(self.list_loopback_devices())
        except Exception as exc:
            system_audio = ()
            errors.append(f"PC音声一覧を取得できません: {exc}")
            logging.getLogger(__name__).warning(
                "Loopback enumeration failed",
                exc_info=exc,
            )
        try:
            screens = tuple(self.list_screen_targets())
        except Exception as exc:
            screens = ()
            errors.append(f"画面一覧を取得できません: {exc}")
            logging.getLogger(__name__).warning(
                "Screen target enumeration failed",
                exc_info=exc,
            )
        self.sources_refreshed.emit(
            request_id,
            CaptureSourcesSnapshot(
                microphones=microphones,
                system_audio=system_audio,
                screens=screens,
                errors=tuple(errors),
            ),
        )

    def start_session(
        self,
        *,
        title: str,
        microphone: AudioDevice | None,
        system_audio: AudioDevice | None,
        screen_target: ScreenTarget | None,
    ) -> Path:
        if self.is_recording:
            raise RuntimeError("既に録音中です")
        if self._start_thread is not None and self._start_thread.is_alive():
            raise RuntimeError("前の録音準備を終了しています")
        if microphone is None and system_audio is None:
            raise ValueError("マイクまたはPC音声を1つ以上選択してください")
        title = title.strip()
        if not title:
            raise ValueError("会議名を入力してください")
        self._storage_monitor.check_start_allowed()

        session = RecordingSession(title=title, status=SessionStatus.PREPARING)
        session.retention = asdict(self._settings.retention)
        self._origin_ns = time.perf_counter_ns()
        session.monotonic_origin_ns = self._origin_ns
        session.started_at = RecordingSession.now_iso()
        if microphone is not None:
            session.audio[ComponentKind.MICROPHONE.value] = asdict(microphone)
        if system_audio is not None:
            session.audio[ComponentKind.SYSTEM_AUDIO.value] = asdict(system_audio)
        if screen_target is not None:
            session.screen = asdict(screen_target)

        paths = self._repository.create(session)
        try:
            session_log = SessionLogWriter(
                paths.session_log,
                session_id=session.id,
                sensitive_values=(
                    title,
                    paths.root,
                    self._app_paths.app_root,
                    microphone.id if microphone is not None else None,
                    microphone.name if microphone is not None else None,
                    system_audio.id if system_audio is not None else None,
                    system_audio.name if system_audio is not None else None,
                    screen_target.id if screen_target is not None else None,
                    screen_target.title if screen_target is not None else None,
                ),
                minimum_level=self._settings.log_level,
            )
        except OSError as exc:
            session.ended_at = RecordingSession.now_iso()
            session.duration_ms = (time.perf_counter_ns() - self._origin_ns) // 1_000_000
            session.status = SessionStatus.FAILED_TO_START
            session.add_warning(
                "SESSION_LOG_OPEN_FAILED",
                "セッションログを作成できませんでした",
                int(session.duration_ms),
            )
            self._repository.save(paths, session)
            raise RuntimeError("セッションログを作成できないため録音を開始できません") from exc
        with self._lock:
            self._session = session
            self._session_paths = paths
            self._session_log = session_log
            self._audio_recorders = {}
            self._screen_recorder = None
            self._audio_unavailable_notified = False
            self._metadata_write_failed_notified = False
            self._screen_disabled_by_storage = False
            self._screenshot_count = 0
            self._startup_cancel = threading.Event()
            self._session_terminal.clear()
            self._finalize_progress_percent = -1
            self._finalize_progress_message = ""
            self._finalize_track_ranges = {}

        self._append_event("session_preparing")
        self._set_component(ComponentKind.SESSION_STORAGE, ComponentStatus.RUNNING)
        self.session_preparing.emit(str(paths.root))
        self._start_thread = threading.Thread(
            target=self._start_session_worker,
            args=(microphone, system_audio, screen_target, paths),
            name="session-startup",
            daemon=True,
        )
        self._start_thread.start()
        return paths.root

    def _start_session_worker(
        self,
        microphone: AudioDevice | None,
        system_audio: AudioDevice | None,
        screen_target: ScreenTarget | None,
        paths: SessionPaths,
    ) -> None:
        audio_start_gate = threading.Event()
        try:
            if self._startup_cancel.is_set():
                self._cancel_session_start(paths, audio_start_gate)
                return
            self._audio_recorders = {}
            if microphone is not None:
                recorder = self._create_audio_recorder(
                    ComponentKind.MICROPHONE,
                    microphone,
                    "microphone",
                    paths,
                    audio_start_gate,
                )
                self._audio_recorders[ComponentKind.MICROPHONE] = recorder
                recorder.start()
            else:
                self._set_component(
                    ComponentKind.MICROPHONE,
                    ComponentStatus.NOT_CONFIGURED,
                )

            if system_audio is not None:
                recorder = self._create_audio_recorder(
                    ComponentKind.SYSTEM_AUDIO,
                    system_audio,
                    "system_audio",
                    paths,
                    audio_start_gate,
                )
                self._audio_recorders[ComponentKind.SYSTEM_AUDIO] = recorder
                recorder.start()
            else:
                self._set_component(
                    ComponentKind.SYSTEM_AUDIO,
                    ComponentStatus.NOT_CONFIGURED,
                )

            ready_audio, cancelled = self._wait_for_audio_initialization()
            if cancelled:
                self._cancel_session_start(paths, audio_start_gate)
                return
            if not ready_audio:
                audio_start_gate.set()
                message = (
                    "マイクとPC音声のどちらも開始できませんでした。デバイスを再選択してください。"
                )
                self._fail_session_start(
                    paths,
                    warning_code="FAILED_TO_START",
                    message=message,
                    event_type="session_start_failed",
                )
                self.session_start_failed.emit(str(paths.root), message)
                return

            with self._lock:
                if self._startup_cancel.is_set():
                    cancelled = True
                else:
                    cancelled = False
                    if screen_target is not None:
                        self._screen_recorder = self._create_screen_recorder(
                            screen_target,
                            paths,
                        )
                        self._screen_recorder.start()
                    else:
                        self._screen_recorder = None
                        self._set_component(
                            ComponentKind.SCREEN,
                            ComponentStatus.NOT_CONFIGURED,
                        )
                    self._storage_monitor.start(
                        low_capacity_callback=self._on_low_disk_space,
                        check_failed_callback=self._on_storage_check_failed,
                    )
                    assert self._session is not None
                    self._session.status = SessionStatus.RECORDING
                    self._try_save_session(
                        paths,
                        self._session,
                        operation="session_started",
                    )
                    audio_start_gate.set()
                    self._append_event(
                        "session_started",
                        audio_components=[kind.value for kind in ready_audio],
                        screen_configured=screen_target is not None,
                    )
                    self.session_started.emit(str(paths.root))
            if cancelled:
                self._cancel_session_start(paths, audio_start_gate)
                return

            self._remember_devices(microphone, system_audio)
            self._notify_if_all_audio_failed()
        except Exception as exc:
            audio_start_gate.set()
            self._log_session_exception(
                "session_start_failed",
                exc,
                component="session",
                error_code="SESSION_START_FAILED",
            )
            message = "録音の準備中にエラーが発生しました。デバイスを再選択してください。"
            self._fail_session_start(
                paths,
                warning_code="SESSION_START_FAILED",
                message=message,
                event_type="session_start_failed",
            )
            self.session_start_failed.emit(str(paths.root), message)
        finally:
            with self._lock:
                if self._start_thread is threading.current_thread():
                    self._start_thread = None

    def replace_screen_target(self, target: ScreenTarget) -> None:
        with self._lock:
            if self._session_log is not None:
                self._session_log.add_sensitive_values(target.id, target.title)
            if self._screen_disabled_by_storage:
                raise RuntimeError(
                    "空き容量不足のため画面保存は停止しています。会議終了後に空き容量を確保してください。"
                )
        if self._screen_recorder is None:
            if self._session_paths is None:
                return
            self._screen_recorder = self._create_screen_recorder(target, self._session_paths)
            self._screen_recorder.start()
        else:
            self._screen_recorder.replace_target(target)
        with self._lock:
            if self._session is not None and self._session_paths is not None:
                self._session.screen = asdict(target)
                self._try_save_session(
                    self._session_paths,
                    self._session,
                    operation="screen_target_replaced",
                )
        self._append_event("screen_target_replaced", target=target.title)

    def stop_session(self) -> None:
        with self._lock:
            if self._session is None:
                return
            if self._session.status == SessionStatus.PREPARING:
                if self._startup_cancel.is_set():
                    return
                self._startup_cancel.set()
                cancel_start = True
            elif self._session.status == SessionStatus.RECORDING:
                cancel_start = False
            else:
                return
        if cancel_start:
            self._append_event("session_start_cancel_requested")
            return
        if self._stop_thread is not None and self._stop_thread.is_alive():
            return
        self._stop_thread = threading.Thread(
            target=self._stop_session_worker,
            name="session-finalize",
            daemon=True,
        )
        self._stop_thread.start()

    def stop_for_shutdown(self, timeout_seconds: float = 4.0) -> bool:
        if timeout_seconds < 0:
            raise ValueError("shutdown timeout must not be negative")
        with self._lock:
            active = self._session is not None and self._session.status in {
                SessionStatus.PREPARING,
                SessionStatus.RECORDING,
                SessionStatus.STOPPING,
                SessionStatus.FINALIZING,
            }
        if not active:
            return True
        self.stop_session()
        return self._session_terminal.wait(timeout=timeout_seconds)

    def _create_audio_recorder(
        self,
        kind: ComponentKind,
        device: AudioDevice,
        track_name: str,
        paths: SessionPaths,
        start_gate: threading.Event,
    ) -> AudioTrackRecorder:
        def state_callback(
            status: ComponentStatus,
            code: str | None,
            message: str | None,
        ) -> None:
            if code == "AUDIO_OPEN_FAILED":
                code = (
                    "MIC_OPEN_FAILED"
                    if kind == ComponentKind.MICROPHONE
                    else "SYSTEM_AUDIO_OPEN_FAILED"
                )
            self._set_component(kind, status, code, message)

        return AudioTrackRecorder(
            backend=self._audio_backend,
            device=device,
            track_name=track_name,
            audio_dir=paths.audio,
            state_callback=state_callback,
            meter_callback=lambda level: self.meter_changed.emit(kind.value, level),
            origin_ns=self._origin_ns,
            start_gate=start_gate,
            exception_callback=lambda code, exc: self._log_audio_exception(kind, code, exc),
            finalize_progress_callback=lambda phase, completed, total: (
                self._on_audio_finalize_progress(kind, phase, completed, total)
            ),
        )

    def _log_audio_exception(
        self,
        kind: ComponentKind,
        error_code: str,
        exception: Exception,
    ) -> None:
        if error_code == "AUDIO_OPEN_FAILED":
            error_code = (
                "MIC_OPEN_FAILED"
                if kind == ComponentKind.MICROPHONE
                else "SYSTEM_AUDIO_OPEN_FAILED"
            )
        self._log_session_exception(
            "worker_exception",
            exception,
            component=kind.value,
            error_code=error_code,
        )

    def _wait_for_audio_initialization(self) -> tuple[list[ComponentKind], bool]:
        deadline = time.monotonic() + self._audio_start_timeout_seconds
        while not all(recorder.startup_complete for recorder in self._audio_recorders.values()):
            if self._startup_cancel.wait(timeout=min(0.05, max(0.0, deadline - time.monotonic()))):
                return [], True
            if time.monotonic() >= deadline:
                break
        if self._startup_cancel.is_set():
            return [], True

        ready: list[ComponentKind] = []
        for kind, recorder in self._audio_recorders.items():
            if recorder.is_ready:
                ready.append(kind)
                continue
            if recorder.startup_complete:
                continue
            code = (
                "MIC_OPEN_TIMEOUT"
                if kind == ComponentKind.MICROPHONE
                else "SYSTEM_AUDIO_OPEN_TIMEOUT"
            )
            recorder.cancel_start(
                code,
                "音声デバイスを "
                f"{self._audio_start_timeout_seconds:g} 秒以内に開始できませんでした",
            )
        return ready, False

    def _cancel_session_start(
        self,
        paths: SessionPaths,
        audio_start_gate: threading.Event,
    ) -> None:
        for recorder in self._audio_recorders.values():
            if recorder.startup_complete:
                recorder.request_stop()
            else:
                recorder.cancel_start(
                    "SESSION_START_CANCELLED",
                    "ユーザー操作により録音準備をキャンセルしました",
                )
        audio_start_gate.set()
        message = "録音の開始をキャンセルしました。"
        self._fail_session_start(
            paths,
            warning_code="SESSION_START_CANCELLED",
            message=message,
            event_type="session_start_cancelled",
        )
        self.session_start_cancelled.emit(str(paths.root))

    def _fail_session_start(
        self,
        paths: SessionPaths,
        *,
        warning_code: str,
        message: str,
        event_type: str,
    ) -> None:
        stats: dict[str, AudioTrackStats] = {}
        failures: list[str] = []
        if self._screen_recorder is not None:
            self._screen_recorder.request_stop()
            try:
                self._screen_recorder.finish()
            except Exception as exc:
                failures.append(f"screen: {exc}")
                self._log_session_exception(
                    "session_start_cleanup_failed",
                    exc,
                    component=ComponentKind.SCREEN.value,
                    error_code="START_CLEANUP_FAILED",
                )
        for kind, recorder in self._audio_recorders.items():
            recorder.request_stop()
            try:
                result = recorder.finish(timeout=self._audio_start_timeout_seconds)
                if result is not None:
                    stats[kind.value] = result
            except Exception as exc:
                failures.append(f"{kind.value}: {exc}")
                self._log_session_exception(
                    "session_start_cleanup_failed",
                    exc,
                    component=kind.value,
                    error_code="START_CLEANUP_FAILED",
                )

        try:
            self._storage_monitor.finish()
        except Exception as exc:
            failures.append(f"storage monitor: {exc}")
            self._log_session_exception(
                "session_start_cleanup_failed",
                exc,
                component=ComponentKind.SESSION_STORAGE.value,
                error_code="START_CLEANUP_FAILED",
            )
        self._set_component(ComponentKind.SESSION_STORAGE, ComponentStatus.STOPPED)
        try:
            self._write_audio_manifest(
                paths.audio / "manifest.json",
                stats,
                monotonic_origin_ns=self._origin_ns,
            )
        except OSError as exc:
            failures.append(f"audio manifest: {exc}")
            self._log_session_exception(
                "session_start_cleanup_failed",
                exc,
                component=ComponentKind.SESSION_STORAGE.value,
                error_code="START_CLEANUP_FAILED",
            )
        ended_ns = time.perf_counter_ns()
        with self._lock:
            assert self._session is not None
            self._session.ended_at = RecordingSession.now_iso()
            self._session.duration_ms = (ended_ns - self._origin_ns) // 1_000_000
            self._session.status = SessionStatus.FAILED_TO_START
            self._session.add_warning(
                warning_code,
                message,
                int(self._session.duration_ms),
            )
            for failure in failures:
                self._session.add_warning(
                    "START_CLEANUP_FAILED", failure, int(self._session.duration_ms)
                )
            self._try_save_session(
                paths,
                self._session,
                operation="session_start_failed",
            )
        self._append_event(
            event_type,
            failure_count=len(failures),
            failures=failures,
        )
        self._close_session_log()
        self._session_terminal.set()

    def _create_screen_recorder(
        self,
        target: ScreenTarget,
        paths: SessionPaths,
    ) -> ScreenRecorder:
        thresholds = self._settings.screen_change_thresholds
        detector = ScreenChangeDetector(
            pixel_diff_threshold=thresholds.pixel_diff_threshold,
            changed_area_ratio_threshold=thresholds.changed_area_ratio_threshold,
            mean_abs_diff_threshold=thresholds.mean_abs_diff_threshold,
            debounce_ms=thresholds.debounce_ms,
            stable_changed_area_ratio=thresholds.stable_changed_area_ratio,
            timeout_ms=thresholds.timeout_ms,
        )
        return ScreenRecorder(
            backend=self._screen_backend,
            target=target,
            store=ScreenshotStore(paths.screenshots),
            origin_ns=self._origin_ns,
            state_callback=lambda status, code, message: self._set_component(
                ComponentKind.SCREEN,
                status,
                code,
                message,
            ),
            count_callback=self._on_screenshot_count,
            evaluation_fps=self._settings.screen_evaluation_fps,
            detector=detector,
            exception_callback=lambda code, exc: self._log_session_exception(
                "worker_exception",
                exc,
                component=ComponentKind.SCREEN.value,
                error_code=code,
            ),
        )

    def _remember_devices(
        self,
        microphone: AudioDevice | None,
        system_audio: AudioDevice | None,
    ) -> None:
        self._settings = replace(
            self._settings,
            last_microphone_device_id=microphone.id if microphone is not None else None,
            last_system_device_id=system_audio.id if system_audio is not None else None,
        )
        try:
            self._settings_repository.save(self._settings)
        except OSError as exc:
            message = f"前回使用したデバイス設定を保存できませんでした: {exc}"
            self._append_event("settings_write_failed", message=message)
            self.fatal_error.emit(message)

    def _stop_session_worker(self) -> None:
        with self._lock:
            session = self._session
            paths = self._session_paths
            if session is None or paths is None:
                return
            session.status = SessionStatus.STOPPING
            self._try_save_session(paths, session, operation="session_stopping")
            audio_kinds = list(self._audio_recorders)
            self._finalize_track_ranges = {
                kind: (
                    15 + (70 * index // max(1, len(audio_kinds))),
                    15 + (70 * (index + 1) // max(1, len(audio_kinds))),
                )
                for index, kind in enumerate(audio_kinds)
            }
        self._emit_finalize_progress(0, "記録の停止を開始しています")
        self._append_event("session_stopping")
        self._storage_monitor.request_stop()

        for recorder in self._audio_recorders.values():
            recorder.request_stop()
        if self._screen_recorder is not None:
            self._screen_recorder.request_stop()
        self._emit_finalize_progress(8, "画面取得を停止しています")

        stats: dict[str, AudioTrackStats] = {}
        failures: list[str] = []
        if self._screen_recorder is not None:
            try:
                self._screen_recorder.finish()
            except Exception as exc:
                failures.append(f"screen: {exc}")
                self._log_session_exception(
                    "finalize_failed",
                    exc,
                    component=ComponentKind.SCREEN.value,
                    error_code="FINALIZE_FAILED",
                )
        self._emit_finalize_progress(12, "画面取得を停止しました")

        with self._lock:
            session.status = SessionStatus.FINALIZING
            self._try_save_session(paths, session, operation="session_finalizing")
        self._emit_finalize_progress(15, "音声ファイルを確定しています")

        for kind, recorder in self._audio_recorders.items():
            start, end = self._finalize_track_ranges[kind]
            self._emit_finalize_progress(start, f"{self._audio_label(kind)}を停止しています")
            try:
                result = recorder.finish()
                if result is not None:
                    stats[kind.value] = result
            except Exception as exc:
                failures.append(f"{kind.value}: {exc}")
                self._log_session_exception(
                    "finalize_failed",
                    exc,
                    component=kind.value,
                    error_code="FINALIZE_FAILED",
                )
                self._set_component(kind, ComponentStatus.FAILED, "FINALIZE_FAILED", str(exc))
            self._emit_finalize_progress(end, f"{self._audio_label(kind)}を確定しました")

        self._emit_finalize_progress(85, "保存状態を確認しています")

        try:
            self._storage_monitor.finish()
        except Exception as exc:
            failures.append(f"storage monitor: {exc}")
            self._log_session_exception(
                "finalize_failed",
                exc,
                component=ComponentKind.SESSION_STORAGE.value,
                error_code="FINALIZE_FAILED",
            )
        self._emit_finalize_progress(90, "音声情報を保存しています")

        with self._lock:
            storage_failed = (
                session.components[ComponentKind.SESSION_STORAGE.value].status
                == ComponentStatus.FAILED
            )
        if not storage_failed:
            self._set_component(ComponentKind.SESSION_STORAGE, ComponentStatus.STOPPED)

        try:
            self._write_audio_manifest(
                paths.audio / "manifest.json",
                stats,
                monotonic_origin_ns=self._origin_ns,
            )
        except OSError as exc:
            failures.append(f"audio manifest: {exc}")
            self._log_session_exception(
                "finalize_failed",
                exc,
                component=ComponentKind.SESSION_STORAGE.value,
                error_code="FINALIZE_FAILED",
            )
        self._emit_finalize_progress(95, "セッション情報を保存しています")
        cleanup_warnings = [
            f"{track}: {track_stats.work_cleanup_error}"
            for track, track_stats in stats.items()
            if track_stats.work_cleanup_error is not None
        ]
        for warning in cleanup_warnings:
            self._append_event("audio_work_cleanup_failed", message=warning)
        ended_ns = time.perf_counter_ns()
        with self._lock:
            session.ended_at = RecordingSession.now_iso()
            session.duration_ms = (ended_ns - self._origin_ns) // 1_000_000
            session.status = SessionStatus.INTERRUPTED if failures else SessionStatus.RECORDED
            for failure in failures:
                session.add_warning("FINALIZE_FAILED", failure, session.duration_ms)
            for warning in cleanup_warnings:
                session.add_warning("AUDIO_WORK_CLEANUP_FAILED", warning, session.duration_ms)
            metadata_saved = self._try_save_session(
                paths,
                session,
                operation="session_finished",
            )
            if not metadata_saved:
                failure = "session metadata: final session.json write failed"
                failures.append(failure)
                session.status = SessionStatus.INTERRUPTED
                session.add_warning("FINALIZE_FAILED", failure, session.duration_ms)
        self._emit_finalize_progress(99, "保存結果を確認しています")
        audio_summary = {
            track: {
                "frames": track_stats.frames_written,
                "segments": track_stats.segments,
                "duration_ms": track_stats.audio_duration_ms,
                "validated": track_stats.validated,
                "overflow_count": track_stats.overflow_count,
                "queue_pressure_count": track_stats.queue_pressure_count,
            }
            for track, track_stats in stats.items()
        }
        self._append_event(
            "session_finished",
            status=session.status.value,
            duration_ms=session.duration_ms,
            screenshot_count=self._screenshot_count,
            audio_summary=audio_summary,
            failure_count=len(failures),
            failures=failures,
        )
        if failures:
            self.fatal_error.emit("一部の記録を正常に確定できませんでした: " + "; ".join(failures))
        self._emit_finalize_progress(
            100,
            "エラーを含む記録を保存しました" if failures else "記録を保存しました",
        )
        self._close_session_log()
        self._session_terminal.set()
        self.session_finished.emit(str(paths.root))

    def _on_audio_finalize_progress(
        self,
        kind: ComponentKind,
        phase: str,
        completed: int,
        total: int,
    ) -> None:
        progress_range = self._finalize_track_ranges.get(kind)
        if progress_range is None:
            return
        start, end = progress_range
        phase_ranges = {
            "stopping_capture": (0.0, 0.1, "録音取得を停止しています"),
            "draining": (0.1, 0.25, "書込み待ち音声を保存しています"),
            "consolidating": (0.25, 0.7, "音声ファイルを結合しています"),
            "validating": (0.7, 0.95, "音声ファイルを検証しています"),
            "cleanup": (0.95, 1.0, "一時ファイルを整理しています"),
        }
        phase_range = phase_ranges.get(phase)
        if phase_range is None:
            return
        phase_start, phase_end, message = phase_range
        ratio = min(1.0, max(0.0, completed / max(1, total)))
        local_ratio = phase_start + ((phase_end - phase_start) * ratio)
        percent = start + round((end - start) * local_ratio)
        self._emit_finalize_progress(
            percent,
            f"{self._audio_label(kind)}: {message}",
        )

    def _emit_finalize_progress(self, percent: int, message: str) -> None:
        percent = min(100, max(0, percent))
        with self._lock:
            if percent < self._finalize_progress_percent:
                return
            if (
                percent == self._finalize_progress_percent
                and message == self._finalize_progress_message
            ):
                return
            self._finalize_progress_percent = percent
            self._finalize_progress_message = message
        self.finalize_progress.emit(percent, message)

    @staticmethod
    def _audio_label(kind: ComponentKind) -> str:
        return "マイク" if kind == ComponentKind.MICROPHONE else "PC音声"

    def _on_low_disk_space(self, capacity: StorageCapacity) -> None:
        with self._lock:
            if self._session is None or self._session.status != SessionStatus.RECORDING:
                return
            if self._screen_disabled_by_storage:
                return
            self._screen_disabled_by_storage = True
            screen_recorder = self._screen_recorder
        message = (
            f"保存先の空き容量が {format_gib(capacity.free_bytes)} GiB まで減少しました。"
            "画面保存を停止し、音声録音を継続します。"
        )
        if screen_recorder is not None:
            screen_recorder.fail("LOW_DISK_SPACE", message)
        self._append_event(
            "low_disk_space",
            free_bytes=capacity.free_bytes,
            minimum_free_bytes=capacity.minimum_free_bytes,
        )
        self._set_component(
            ComponentKind.SESSION_STORAGE,
            ComponentStatus.FAILED,
            "LOW_DISK_SPACE",
            message,
        )
        self.fatal_error.emit(message)

    def _on_screenshot_count(self, count: int) -> None:
        with self._lock:
            self._screenshot_count = max(0, int(count))
            current_count = self._screenshot_count
        self.screenshot_count_changed.emit(current_count)

    def _on_storage_check_failed(self, error: StorageCapacityCheckError) -> None:
        with self._lock:
            if self._session is None or self._session.status != SessionStatus.RECORDING:
                return
        message = f"録音中に保存先の空き容量を確認できなくなりました。音声録音は継続します。{error}"
        self._append_event("storage_capacity_check_failed", message=str(error))
        self._set_component(
            ComponentKind.SESSION_STORAGE,
            ComponentStatus.FAILED,
            "STORAGE_CAPACITY_CHECK_FAILED",
            message,
        )
        self.fatal_error.emit(message)

    def _set_component(
        self,
        kind: ComponentKind,
        status: ComponentStatus,
        error_code: str | None = None,
        message: str | None = None,
    ) -> None:
        with self._lock:
            if self._session is None or self._session_paths is None:
                return
            self._session.set_component(
                kind,
                status,
                error_code=error_code,
                message=message,
            )
            timestamp_ms = (time.perf_counter_ns() - self._origin_ns) // 1_000_000
            if error_code:
                self._session.add_warning(error_code, message or error_code, int(timestamp_ms))
            event_saved = self._try_append_repository_event(
                self._session_paths,
                {
                    "schema_version": 1,
                    "timestamp_ms": int(timestamp_ms),
                    "type": "component_state_changed",
                    "component": kind.value,
                    "status": status.value,
                    "error_code": error_code,
                    "message": message,
                },
                operation="component_state_changed_event",
            )
            metadata_saved = self._try_save_session(
                self._session_paths,
                self._session,
                operation="component_state_changed_session",
            )
            session_log = self._session_log
            effective_state = self._session.components[kind.value]
        if session_log is not None:
            level = (
                "ERROR"
                if status == ComponentStatus.FAILED
                else "WARNING"
                if error_code is not None
                or status in {ComponentStatus.RECONNECTING, ComponentStatus.PAUSED}
                else "INFO"
            )
            session_log.write(
                "component_state_changed",
                level=level,
                timestamp_ms=int(timestamp_ms),
                component=kind.value,
                status=status.value,
                error_code=error_code,
                message=message,
            )
        if not (kind == ComponentKind.SESSION_STORAGE and (not event_saved or not metadata_saved)):
            self.component_changed.emit(
                kind.value,
                effective_state.status.value,
                effective_state.message or "",
            )
        self._notify_if_all_audio_failed()

    def _notify_if_all_audio_failed(self) -> None:
        with self._lock:
            if (
                self._session is None
                or self._session.status != SessionStatus.RECORDING
                or self._audio_unavailable_notified
            ):
                return
            selected = [
                kind
                for kind in (ComponentKind.MICROPHONE, ComponentKind.SYSTEM_AUDIO)
                if kind.value in self._session.audio
            ]
            if not selected or not all(
                self._session.components[kind.value].status == ComponentStatus.FAILED
                for kind in selected
            ):
                return
            self._audio_unavailable_notified = True
        self.fatal_error.emit(
            "選択したすべての音声取得が停止しています。画面取得のみでは会議音声を記録できません。"
        )

    def _append_event(self, event_type: str, **extra: object) -> None:
        with self._lock:
            if self._session_paths is None:
                return
            timestamp_ms = (time.perf_counter_ns() - self._origin_ns) // 1_000_000
            self._try_append_repository_event(
                self._session_paths,
                {
                    "schema_version": 1,
                    "timestamp_ms": int(timestamp_ms),
                    "type": event_type,
                    **extra,
                },
                operation=event_type,
            )
            session_log = self._session_log
        if session_log is not None:
            session_log.write(
                event_type,
                level=self._session_event_level(event_type),
                timestamp_ms=int(timestamp_ms),
                **extra,
            )

    def _try_save_session(
        self,
        paths: SessionPaths,
        session: RecordingSession,
        *,
        operation: str,
    ) -> bool:
        try:
            self._repository.save(paths, session)
        except OSError as exc:
            self._handle_metadata_write_failure(exc, operation=operation)
            return False
        return True

    def _try_append_repository_event(
        self,
        paths: SessionPaths,
        event: dict[str, object],
        *,
        operation: str,
    ) -> bool:
        try:
            self._repository.append_event(paths, event)
        except OSError as exc:
            self._handle_metadata_write_failure(exc, operation=operation)
            return False
        return True

    def _handle_metadata_write_failure(self, error: OSError, *, operation: str) -> None:
        message = (
            "セッション情報を保存できません。音声録音を可能な限り継続しています。"
            "保存先を確認してください。"
        )
        with self._lock:
            timestamp_ms = (time.perf_counter_ns() - self._origin_ns) // 1_000_000
            first_failure = not self._metadata_write_failed_notified
            if self._session is not None:
                self._session.set_component(
                    ComponentKind.SESSION_STORAGE,
                    ComponentStatus.FAILED,
                    error_code="SESSION_METADATA_WRITE_FAILED",
                    message=message,
                )
            if first_failure:
                self._metadata_write_failed_notified = True
                if self._session is not None:
                    self._session.add_warning(
                        "SESSION_METADATA_WRITE_FAILED",
                        message,
                        int(timestamp_ms),
                    )
            session_log = self._session_log
        logging.getLogger(__name__).error(
            "Session metadata write failed operation=%s",
            operation,
            exc_info=error,
        )
        if session_log is not None:
            session_log.write_exception(
                "metadata_write_failed",
                error,
                component=ComponentKind.SESSION_STORAGE.value,
                error_code="SESSION_METADATA_WRITE_FAILED",
                timestamp_ms=int(timestamp_ms),
            )
        if first_failure:
            self.component_changed.emit(
                ComponentKind.SESSION_STORAGE.value,
                ComponentStatus.FAILED.value,
                message,
            )
            self.fatal_error.emit(message)

    def _log_session_exception(
        self,
        event_type: str,
        exception: BaseException,
        *,
        component: str,
        error_code: str,
    ) -> None:
        with self._lock:
            session_log = self._session_log
            timestamp_ms = (time.perf_counter_ns() - self._origin_ns) // 1_000_000
        if session_log is not None:
            session_log.write_exception(
                event_type,
                exception,
                component=component,
                error_code=error_code,
                timestamp_ms=int(timestamp_ms),
            )

    def _close_session_log(self) -> None:
        with self._lock:
            session_log = self._session_log
            self._session_log = None
        if session_log is not None:
            session_log.close()

    @staticmethod
    def _session_event_level(event_type: str) -> str:
        if event_type in {"low_disk_space", "session_start_failed"}:
            return "ERROR"
        if "failed" in event_type:
            return "WARNING"
        return "INFO"

    @staticmethod
    def _write_audio_manifest(
        path: Path,
        stats: dict[str, AudioTrackStats],
        *,
        monotonic_origin_ns: int,
    ) -> None:
        value = {
            "schema_version": AUDIO_MANIFEST_SCHEMA_VERSION,
            "monotonic_origin_ns": monotonic_origin_ns,
            "tracks": {name: asdict(track_stats) for name, track_stats in stats.items()},
        }
        temporary = path.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
