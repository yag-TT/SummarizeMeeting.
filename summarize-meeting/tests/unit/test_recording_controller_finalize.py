from __future__ import annotations

import json
import time
import wave
from pathlib import Path

from summarize_meeting.application.recording_controller import RecordingController
from summarize_meeting.domain.session import (
    ComponentKind,
    ComponentStatus,
    RecordingSession,
    SessionStatus,
)
from summarize_meeting.infrastructure.audio_writer import (
    AudioTrackStats,
    WaveValidationError,
)
from summarize_meeting.infrastructure.paths import PortableAppPaths
from summarize_meeting.infrastructure.session_repository import FileSessionRepository


class _NoopStorageMonitor:
    def request_stop(self) -> None:
        pass

    def finish(self) -> None:
        pass


class _UnexpectedlyFailingStorageMonitor(_NoopStorageMonitor):
    def request_stop(self) -> None:
        raise RuntimeError("unexpected monitor failure")


class _FakeAudioRecorder:
    def __init__(
        self,
        *,
        result: AudioTrackStats | None = None,
        error: Exception | None = None,
    ) -> None:
        self._result = result
        self._error = error

    def request_stop(self) -> None:
        pass

    def finish(self) -> AudioTrackStats | None:
        if self._error is not None:
            raise self._error
        return self._result


class _FailNthSaveRepository:
    def __init__(self, delegate: FileSessionRepository, *, fail_on_call: int) -> None:
        self._delegate = delegate
        self._fail_on_call = fail_on_call
        self.save_calls = 0

    def save(self, paths, session) -> None:
        self.save_calls += 1
        if self.save_calls == self._fail_on_call:
            raise OSError("session.json is temporarily unavailable")
        self._delegate.save(paths, session)

    def append_event(self, paths, event) -> None:
        self._delegate.append_event(paths, event)


def _prepare_controller(tmp_path: Path) -> tuple[RecordingController, Path]:
    app_paths = PortableAppPaths(tmp_path)
    app_paths.ensure_writable()
    controller = RecordingController(
        app_paths,
        storage_monitor=_NoopStorageMonitor(),  # type: ignore[arg-type]
    )
    session = RecordingSession(title="finalize", status=SessionStatus.RECORDING)
    session.audio[ComponentKind.MICROPHONE.value] = {"id": "mic"}
    session.set_component(ComponentKind.MICROPHONE, ComponentStatus.RUNNING)
    session.set_component(ComponentKind.SESSION_STORAGE, ComponentStatus.RUNNING)
    paths = controller._repository.create(session)  # noqa: SLF001
    controller._session = session  # noqa: SLF001
    controller._session_paths = paths  # noqa: SLF001
    controller._origin_ns = time.perf_counter_ns()  # noqa: SLF001
    return controller, paths.root


def test_finalize_validation_failure_interrupts_session(tmp_path: Path) -> None:
    controller, session_root = _prepare_controller(tmp_path)
    controller._audio_recorders = {  # type: ignore[dict-item]  # noqa: SLF001
        ComponentKind.MICROPHONE: _FakeAudioRecorder(
            error=WaveValidationError("Final WAV cannot be opened")
        )
    }

    controller._stop_session_worker()  # noqa: SLF001

    metadata = json.loads((session_root / "session.json").read_text(encoding="utf-8"))
    assert metadata["status"] == SessionStatus.INTERRUPTED
    assert metadata["components"][ComponentKind.MICROPHONE.value]["status"] == (
        ComponentStatus.FAILED
    )
    assert metadata["components"][ComponentKind.MICROPHONE.value]["error_code"] == (
        "FINALIZE_FAILED"
    )
    assert any(warning["code"] == "FINALIZE_FAILED" for warning in metadata["warnings"])


def test_finalize_cleanup_failure_is_recorded_as_warning(tmp_path: Path) -> None:
    controller, session_root = _prepare_controller(tmp_path)
    stats = AudioTrackStats(
        file="microphone.wav",
        sample_rate=48_000,
        channels=1,
        sample_width_bytes=2,
        frames_written=48_000,
        segments=1,
        audio_duration_ms=1_000.0,
        validated=True,
        work_files_removed=False,
        work_cleanup_error="work directory is busy",
    )
    controller._audio_recorders = {  # type: ignore[dict-item]  # noqa: SLF001
        ComponentKind.MICROPHONE: _FakeAudioRecorder(result=stats)
    }

    controller._stop_session_worker()  # noqa: SLF001

    metadata = json.loads((session_root / "session.json").read_text(encoding="utf-8"))
    assert metadata["status"] == SessionStatus.RECORDED
    warning = next(
        warning
        for warning in metadata["warnings"]
        if warning["code"] == "AUDIO_WORK_CLEANUP_FAILED"
    )
    assert "work directory is busy" in warning["message"]
    manifest = json.loads((session_root / "audio" / "manifest.json").read_text(encoding="utf-8"))
    assert not manifest["tracks"]["microphone"]["work_files_removed"]


def test_finalize_progress_is_monotonic_and_completes(tmp_path: Path) -> None:
    controller, _session_root = _prepare_controller(tmp_path)
    controller._audio_recorders = {  # type: ignore[dict-item]  # noqa: SLF001
        ComponentKind.MICROPHONE: _FakeAudioRecorder(result=None)
    }
    progress: list[tuple[int, str]] = []
    controller.finalize_progress.connect(
        lambda percent, message: progress.append((percent, message))
    )

    controller._stop_session_worker()  # noqa: SLF001

    percents = [percent for percent, _ in progress]
    assert percents == sorted(percents)
    assert percents[0] == 0
    assert percents[-1] == 100
    assert any("音声" in message for _, message in progress)


def test_audio_finalize_progress_maps_each_phase_without_regressing(
    tmp_path: Path,
) -> None:
    controller, _session_root = _prepare_controller(tmp_path)
    controller._finalize_track_ranges = {  # noqa: SLF001
        ComponentKind.MICROPHONE: (15, 85)
    }
    progress: list[tuple[int, str]] = []
    controller.finalize_progress.connect(
        lambda percent, message: progress.append((percent, message))
    )

    for phase in (
        "stopping_capture",
        "draining",
        "consolidating",
        "validating",
        "cleanup",
    ):
        controller._on_audio_finalize_progress(  # noqa: SLF001
            ComponentKind.MICROPHONE,
            phase,
            0,
            10,
        )
        controller._on_audio_finalize_progress(  # noqa: SLF001
            ComponentKind.MICROPHONE,
            phase,
            10,
            10,
        )

    percents = [percent for percent, _ in progress]
    assert percents == sorted(percents)
    assert percents[0] == 15
    assert percents[-1] == 85
    assert any("結合" in message for _, message in progress)
    assert any("検証" in message for _, message in progress)


def test_shutdown_stop_requests_stop_and_reports_bounded_wait_timeout(
    tmp_path: Path,
    monkeypatch,
) -> None:
    controller, _session_root = _prepare_controller(tmp_path)
    controller._session_terminal.clear()  # noqa: SLF001
    stop_requests: list[bool] = []
    monkeypatch.setattr(controller, "stop_session", lambda: stop_requests.append(True))

    completed = controller.stop_for_shutdown(timeout_seconds=0.0)

    assert not completed
    assert stop_requests == [True]

    controller._session_terminal.set()  # noqa: SLF001
    assert controller.stop_for_shutdown(timeout_seconds=0.0)


def test_unexpected_finalize_error_still_completes_terminal_flow(tmp_path: Path) -> None:
    controller, session_root = _prepare_controller(tmp_path)
    controller._storage_monitor = _UnexpectedlyFailingStorageMonitor()  # type: ignore[assignment]  # noqa: SLF001,E501
    errors: list[str] = []
    finished: list[str] = []
    controller.fatal_error.connect(errors.append)
    controller.session_finished.connect(finished.append)

    controller._stop_session_worker()  # noqa: SLF001

    metadata = json.loads((session_root / "session.json").read_text(encoding="utf-8"))
    assert metadata["status"] == SessionStatus.INTERRUPTED
    assert controller._session_terminal.is_set()  # noqa: SLF001
    assert finished == [str(session_root)]
    assert any("予期しないエラー" in message for message in errors)


def test_final_metadata_write_failure_does_not_abort_finalize_worker(
    tmp_path: Path,
) -> None:
    controller, session_root = _prepare_controller(tmp_path)
    repository = _FailNthSaveRepository(
        controller._repository,  # noqa: SLF001
        fail_on_call=4,
    )
    controller._repository = repository  # type: ignore[assignment]  # noqa: SLF001
    controller._audio_recorders = {  # type: ignore[dict-item]  # noqa: SLF001
        ComponentKind.MICROPHONE: _FakeAudioRecorder(result=None)
    }
    errors: list[str] = []
    finished: list[str] = []
    controller.fatal_error.connect(errors.append)
    controller.session_finished.connect(finished.append)

    controller._stop_session_worker()  # noqa: SLF001

    assert repository.save_calls == 4
    assert controller._session is not None  # noqa: SLF001
    assert controller._session.status == SessionStatus.INTERRUPTED  # noqa: SLF001
    assert (
        controller._session.components[  # noqa: SLF001
            ComponentKind.SESSION_STORAGE.value
        ].error_code
        == "SESSION_METADATA_WRITE_FAILED"
    )
    assert any(  # noqa: SLF001
        warning["code"] == "FINALIZE_FAILED" for warning in controller._session.warnings
    )
    assert controller._session_terminal.is_set()  # noqa: SLF001
    assert finished == [str(session_root)]
    assert any("セッション情報を保存できません" in error for error in errors)
    persisted = json.loads((session_root / "session.json").read_text(encoding="utf-8"))
    assert persisted["status"] == SessionStatus.FINALIZING


def test_audio_manifest_write_failure_keeps_final_wav_and_completes_terminal_flow(
    tmp_path: Path,
    monkeypatch,
) -> None:
    controller, session_root = _prepare_controller(tmp_path)
    audio_path = session_root / "audio" / "microphone.wav"
    with wave.open(str(audio_path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(100)
        stream.writeframes(b"\0\0" * 20)
    stats = AudioTrackStats(
        file="microphone.wav",
        sample_rate=100,
        channels=1,
        sample_width_bytes=2,
        frames_written=20,
        segments=1,
        audio_duration_ms=200.0,
        validated=True,
        work_files_removed=True,
    )
    controller._audio_recorders = {  # type: ignore[dict-item]  # noqa: SLF001
        ComponentKind.MICROPHONE: _FakeAudioRecorder(result=stats)
    }

    def fail_manifest(*_args, **_kwargs) -> None:
        raise OSError("manifest destination is unavailable")

    monkeypatch.setattr(
        RecordingController,
        "_write_audio_manifest",
        staticmethod(fail_manifest),
    )
    errors: list[str] = []
    finished: list[str] = []
    controller.fatal_error.connect(errors.append)
    controller.session_finished.connect(finished.append)

    controller._stop_session_worker()  # noqa: SLF001

    assert audio_path.is_file()
    with wave.open(str(audio_path), "rb") as stream:
        assert stream.getnframes() == 20
    assert not (session_root / "audio" / "manifest.json").exists()
    persisted = json.loads((session_root / "session.json").read_text(encoding="utf-8"))
    assert persisted["status"] == SessionStatus.INTERRUPTED
    assert any(
        warning["code"] == "FINALIZE_FAILED" and "audio manifest" in warning["message"]
        for warning in persisted["warnings"]
    )
    assert controller._session_terminal.is_set()  # noqa: SLF001
    assert finished == [str(session_root)]
    assert any("audio manifest" in error for error in errors)
