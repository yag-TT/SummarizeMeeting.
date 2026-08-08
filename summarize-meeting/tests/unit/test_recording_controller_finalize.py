from __future__ import annotations

import json
import time
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


class _NoopStorageMonitor:
    def request_stop(self) -> None:
        pass

    def finish(self) -> None:
        pass


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
        file="audio/microphone.wav",
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
