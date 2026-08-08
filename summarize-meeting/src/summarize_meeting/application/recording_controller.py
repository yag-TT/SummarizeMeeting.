from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, replace
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
from summarize_meeting.capture.screen.recorder import ScreenRecorder
from summarize_meeting.capture.screen.windows_wgc import WindowsWgcScreenBackend
from summarize_meeting.domain.capture import AudioDevice, ScreenTarget
from summarize_meeting.domain.session import (
    ComponentKind,
    ComponentStatus,
    RecordingSession,
    SessionStatus,
)
from summarize_meeting.infrastructure.audio_writer import AudioTrackStats
from summarize_meeting.infrastructure.paths import PortableAppPaths
from summarize_meeting.infrastructure.screenshot_store import ScreenshotStore
from summarize_meeting.infrastructure.session_repository import (
    FileSessionRepository,
    SessionPaths,
)
from summarize_meeting.infrastructure.settings import AppSettings, FileSettingsRepository
from summarize_meeting.infrastructure.storage_probe import SystemStorageProbe


class RecordingController(QObject):
    component_changed = Signal(str, str, str)
    meter_changed = Signal(str, float)
    screenshot_count_changed = Signal(int)
    session_started = Signal(str)
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
        self._screen_backend = WindowsWgcScreenBackend()
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
        self._origin_ns = 0
        self._audio_recorders: dict[ComponentKind, AudioTrackRecorder] = {}
        self._screen_recorder: ScreenRecorder | None = None
        self._lock = threading.RLock()
        self._stop_thread: threading.Thread | None = None
        self._audio_unavailable_notified = False
        self._screen_disabled_by_storage = False

    @property
    def last_microphone_device_id(self) -> str | None:
        return self._settings.last_microphone_device_id

    @property
    def last_system_device_id(self) -> str | None:
        return self._settings.last_system_device_id

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
        with self._lock:
            self._session = session
            self._session_paths = paths
            self._audio_unavailable_notified = False
            self._screen_disabled_by_storage = False

        self._append_event("session_preparing")
        self._set_component(ComponentKind.SESSION_STORAGE, ComponentStatus.RUNNING)
        self._audio_recorders = {}
        audio_start_gate = threading.Event()
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
            self._set_component(ComponentKind.MICROPHONE, ComponentStatus.NOT_CONFIGURED)

        if system_audio is not None:
            recorder = self._create_audio_recorder(
                ComponentKind.SYSTEM_AUDIO,
                system_audio,
                "system",
                paths,
                audio_start_gate,
            )
            self._audio_recorders[ComponentKind.SYSTEM_AUDIO] = recorder
            recorder.start()
        else:
            self._set_component(ComponentKind.SYSTEM_AUDIO, ComponentStatus.NOT_CONFIGURED)

        ready_audio = self._wait_for_audio_initialization()
        if not ready_audio:
            audio_start_gate.set()
            self._fail_session_start(paths)
            raise RuntimeError(
                "マイクとPC音声のどちらも開始できませんでした。デバイスを再選択してください。"
            )

        audio_start_gate.set()
        if screen_target is not None:
            self._screen_recorder = self._create_screen_recorder(screen_target, paths)
            self._screen_recorder.start()
        else:
            self._screen_recorder = None
            self._set_component(ComponentKind.SCREEN, ComponentStatus.NOT_CONFIGURED)

        with self._lock:
            session.status = SessionStatus.RECORDING
            self._repository.save(paths, session)
        self._append_event("session_started")
        self._storage_monitor.start(
            low_capacity_callback=self._on_low_disk_space,
            check_failed_callback=self._on_storage_check_failed,
        )
        self.session_started.emit(str(paths.root))
        self._remember_devices(microphone, system_audio)
        self._notify_if_all_audio_failed()
        return paths.root

    def replace_screen_target(self, target: ScreenTarget) -> None:
        with self._lock:
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
                self._repository.save(self._session_paths, self._session)
        self._append_event("screen_target_replaced", target=target.title)

    def stop_session(self) -> None:
        if not self.is_recording:
            return
        if self._stop_thread is not None and self._stop_thread.is_alive():
            return
        self._stop_thread = threading.Thread(
            target=self._stop_session_worker,
            name="session-finalize",
            daemon=True,
        )
        self._stop_thread.start()

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
        )

    def _wait_for_audio_initialization(self) -> list[ComponentKind]:
        deadline = time.monotonic() + self._audio_start_timeout_seconds
        for recorder in self._audio_recorders.values():
            recorder.wait_until_initialized(max(0.0, deadline - time.monotonic()))

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
        return ready

    def _fail_session_start(self, paths: SessionPaths) -> None:
        stats: dict[str, AudioTrackStats] = {}
        failures: list[str] = []
        for kind, recorder in self._audio_recorders.items():
            recorder.request_stop()
            try:
                result = recorder.finish(timeout=self._audio_start_timeout_seconds)
                if result is not None:
                    stats[kind.value] = result
            except Exception as exc:
                failures.append(f"{kind.value}: {exc}")

        self._set_component(ComponentKind.SESSION_STORAGE, ComponentStatus.STOPPED)
        self._write_audio_manifest(
            paths.audio / "manifest.json",
            stats,
            monotonic_origin_ns=self._origin_ns,
        )
        ended_ns = time.perf_counter_ns()
        with self._lock:
            assert self._session is not None
            self._session.ended_at = RecordingSession.now_iso()
            self._session.duration_ms = (ended_ns - self._origin_ns) // 1_000_000
            self._session.status = SessionStatus.FAILED_TO_START
            self._session.add_warning(
                "FAILED_TO_START",
                "選択した音声デバイスを開始できませんでした",
                int(self._session.duration_ms),
            )
            for failure in failures:
                self._session.add_warning(
                    "START_CLEANUP_FAILED", failure, int(self._session.duration_ms)
                )
            self._repository.save(paths, self._session)
        self._append_event("session_start_failed", failures=failures)

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
            count_callback=self.screenshot_count_changed.emit,
            evaluation_fps=self._settings.screen_evaluation_fps,
            detector=detector,
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
            self._repository.save(paths, session)
        self._append_event("session_stopping")
        self._storage_monitor.request_stop()

        for recorder in self._audio_recorders.values():
            recorder.request_stop()
        if self._screen_recorder is not None:
            self._screen_recorder.request_stop()

        stats: dict[str, AudioTrackStats] = {}
        failures: list[str] = []
        if self._screen_recorder is not None:
            try:
                self._screen_recorder.finish()
            except Exception as exc:
                failures.append(f"screen: {exc}")

        with self._lock:
            session.status = SessionStatus.FINALIZING
            self._repository.save(paths, session)

        for kind, recorder in self._audio_recorders.items():
            try:
                result = recorder.finish()
                if result is not None:
                    stats[kind.value] = result
            except Exception as exc:
                failures.append(f"{kind.value}: {exc}")
                self._set_component(kind, ComponentStatus.FAILED, "FINALIZE_FAILED", str(exc))

        try:
            self._storage_monitor.finish()
        except Exception as exc:
            failures.append(f"storage monitor: {exc}")

        with self._lock:
            storage_failed = (
                session.components[ComponentKind.SESSION_STORAGE.value].status
                == ComponentStatus.FAILED
            )
        if not storage_failed:
            self._set_component(ComponentKind.SESSION_STORAGE, ComponentStatus.STOPPED)

        self._write_audio_manifest(
            paths.audio / "manifest.json",
            stats,
            monotonic_origin_ns=self._origin_ns,
        )
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
                session.add_warning(
                    "AUDIO_WORK_CLEANUP_FAILED", warning, session.duration_ms
                )
            self._repository.save(paths, session)
        self._append_event("session_finished", failures=failures)
        if failures:
            self.fatal_error.emit("一部の記録を正常に確定できませんでした: " + "; ".join(failures))
        self.session_finished.emit(str(paths.root))

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
            self._repository.append_event(
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
            )
            self._repository.save(self._session_paths, self._session)
        self.component_changed.emit(kind.value, status.value, message or "")
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
            self._repository.append_event(
                self._session_paths,
                {
                    "schema_version": 1,
                    "timestamp_ms": int(timestamp_ms),
                    "type": event_type,
                    **extra,
                },
            )

    @staticmethod
    def _write_audio_manifest(
        path: Path,
        stats: dict[str, AudioTrackStats],
        *,
        monotonic_origin_ns: int,
    ) -> None:
        value = {
            "schema_version": 1,
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
