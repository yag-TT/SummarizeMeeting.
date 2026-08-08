from __future__ import annotations

import json
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from summarize_meeting.application.recording_controller import RecordingController
from summarize_meeting.application.storage_monitor import StorageMonitor
from summarize_meeting.domain.capture import AudioDevice, AudioFormat
from summarize_meeting.domain.session import ComponentKind, ComponentStatus, SessionStatus
from summarize_meeting.infrastructure.paths import PortableAppPaths


class _SequenceProbe:
    def __init__(self, *values: int) -> None:
        self._values = deque(values)

    def free_bytes(self, path: Path) -> int:
        return self._values.popleft()


class _FakeAudioStream:
    audio_format = AudioFormat(sample_rate=8_000, channels=1)

    def read(self, frames: int) -> np.ndarray:
        time.sleep(0.005)
        return np.full((frames, 1), 0.1, dtype=np.float32)

    def close(self) -> None:
        pass


class _SelectiveAudioBackend:
    def __init__(self, failing_device_ids: set[str]) -> None:
        self._failing_device_ids = failing_device_ids

    def list_input_devices(self) -> list[AudioDevice]:
        return []

    def list_loopback_devices(self) -> list[AudioDevice]:
        return []

    def open_stream(
        self,
        device_id: str,
        *,
        sample_rate: int,
        block_frames: int,
    ) -> _FakeAudioStream:
        if device_id in self._failing_device_ids:
            raise RuntimeError(f"cannot open {device_id}")
        return _FakeAudioStream()


class _BlockingOpenAudioBackend(_SelectiveAudioBackend):
    def __init__(self) -> None:
        super().__init__(set())
        self.open_started = threading.Event()
        self.release_open = threading.Event()

    def open_stream(
        self,
        device_id: str,
        *,
        sample_rate: int,
        block_frames: int,
    ) -> _FakeAudioStream:
        self.open_started.set()
        if not self.release_open.wait(2.0):
            raise TimeoutError("test did not release audio open")
        return super().open_stream(
            device_id,
            sample_rate=sample_rate,
            block_frames=block_frames,
        )


def _make_controller(
    tmp_path: Path,
    *,
    failing_device_ids: set[str],
) -> RecordingController:
    paths = PortableAppPaths(tmp_path)
    paths.ensure_writable()
    monitor = StorageMonitor(
        path=paths.meetings_dir,
        probe=_SequenceProbe(10 * 1024**3),
        check_interval_seconds=60.0,
    )
    controller = RecordingController(
        paths,
        storage_monitor=monitor,
        audio_start_timeout_seconds=1.0,
    )
    controller._audio_backend = _SelectiveAudioBackend(  # type: ignore[assignment]  # noqa: SLF001
        failing_device_ids
    )
    return controller


def _wait_for(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        if QCoreApplication.instance() is not None:
            QCoreApplication.processEvents()
        time.sleep(0.01)
    if QCoreApplication.instance() is not None:
        QCoreApplication.processEvents()
    assert predicate()


def test_session_starts_when_one_of_two_audio_sources_is_ready(tmp_path: Path) -> None:
    controller = _make_controller(tmp_path, failing_device_ids={"mic"})

    session_path = controller.start_session(
        title="partial audio",
        microphone=AudioDevice("mic", "Broken mic", 1),
        system_audio=AudioDevice("system", "Working output", 1),
        screen_target=None,
    )

    _wait_for(
        lambda: (
            controller._session is not None  # noqa: SLF001
            and controller._session.components[  # noqa: SLF001
                ComponentKind.SYSTEM_AUDIO.value
            ].status
            == ComponentStatus.RUNNING
        )
    )
    session = controller._session  # noqa: SLF001
    assert session is not None
    assert session.status == SessionStatus.RECORDING
    assert session.components[ComponentKind.MICROPHONE.value].status == ComponentStatus.FAILED
    assert session.components[ComponentKind.MICROPHONE.value].error_code == "MIC_OPEN_FAILED"

    controller.stop_session()
    _wait_for(
        lambda: not controller.is_recording and controller._session_log is None  # noqa: SLF001
    )

    metadata = json.loads((session_path / "session.json").read_text(encoding="utf-8"))
    assert metadata["status"] == SessionStatus.RECORDED
    assert (session_path / "audio" / "system.wav").is_file()
    session_log = (session_path / "logs" / "session.log").read_text(encoding="utf-8")
    assert "partial audio" not in session_log
    assert "Broken mic" not in session_log
    assert "Working output" not in session_log
    log_entries = [json.loads(line) for line in session_log.splitlines()]
    assert log_entries[0]["event"] == "session_preparing"
    assert any(entry["event"] == "component_state_changed" for entry in log_entries)
    worker_error = next(
        entry
        for entry in log_entries
        if entry["event"] == "worker_exception"
        and entry["details"]["component"] == ComponentKind.MICROPHONE.value
    )
    assert worker_error["details"]["error_code"] == "MIC_OPEN_FAILED"
    assert worker_error["details"]["exception_type"] == "RuntimeError"
    assert "[REDACTED]" in worker_error["details"]["stack_trace"]
    assert log_entries[-1]["event"] == "session_finished"
    assert log_entries[-1]["details"]["screenshot_count"] == 0
    system_summary = log_entries[-1]["details"]["audio_summary"]["system_audio"]
    assert system_summary["frames"] > 0
    assert system_summary["validated"]
    assert controller._session_terminal.is_set()  # noqa: SLF001


def test_session_is_failed_to_start_when_all_audio_sources_fail(
    tmp_path: Path,
    qapp: QApplication,
) -> None:
    controller = _make_controller(tmp_path, failing_device_ids={"mic", "system"})
    start_failures: list[tuple[str, str]] = []
    controller.session_start_failed.connect(
        lambda path, message: start_failures.append((path, message))
    )

    controller.start_session(
        title="no audio",
        microphone=AudioDevice("mic", "Broken mic", 1),
        system_audio=AudioDevice("system", "Broken output", 1),
        screen_target=None,
    )
    _wait_for(
        lambda: not controller.is_recording and controller._session_log is None  # noqa: SLF001
    )

    meeting_dirs = list((tmp_path / "data" / "meetings").iterdir())
    assert len(meeting_dirs) == 1
    metadata = json.loads((meeting_dirs[0] / "session.json").read_text(encoding="utf-8"))
    assert metadata["status"] == SessionStatus.FAILED_TO_START
    assert metadata["ended_at"] is not None
    assert metadata["components"][ComponentKind.MICROPHONE.value]["error_code"] == (
        "MIC_OPEN_FAILED"
    )
    assert metadata["components"][ComponentKind.SYSTEM_AUDIO.value]["error_code"] == (
        "SYSTEM_AUDIO_OPEN_FAILED"
    )
    events = [
        json.loads(line)
        for line in (meeting_dirs[0] / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert events[-1]["type"] == "session_start_failed"
    session_log = (meeting_dirs[0] / "logs" / "session.log").read_text(encoding="utf-8")
    assert "no audio" not in session_log
    assert "Broken mic" not in session_log
    assert "Broken output" not in session_log
    log_entries = [json.loads(line) for line in session_log.splitlines()]
    assert log_entries[-1]["event"] == "session_start_failed"
    _wait_for(lambda: bool(start_failures))
    assert start_failures and "どちらも開始できません" in start_failures[0][1]
    assert not controller.is_recording
    assert controller._session_terminal.is_set()  # noqa: SLF001


def test_start_session_returns_while_audio_device_is_still_opening(
    tmp_path: Path,
) -> None:
    controller = _make_controller(tmp_path, failing_device_ids=set())
    backend = _BlockingOpenAudioBackend()
    controller._audio_backend = backend  # type: ignore[assignment]  # noqa: SLF001

    started_at = time.monotonic()
    session_path = controller.start_session(
        title="responsive startup",
        microphone=AudioDevice("slow-mic", "Slow microphone", 1),
        system_audio=None,
        screen_target=None,
    )
    elapsed = time.monotonic() - started_at

    assert elapsed < 0.5
    assert backend.open_started.wait(1.0)
    assert controller._session is not None  # noqa: SLF001
    assert controller._session.status == SessionStatus.PREPARING  # noqa: SLF001

    backend.release_open.set()
    _wait_for(
        lambda: (
            controller._session is not None  # noqa: SLF001
            and controller._session.status == SessionStatus.RECORDING
        )  # noqa: SLF001
    )
    controller.stop_session()
    _wait_for(
        lambda: not controller.is_recording and controller._session_log is None  # noqa: SLF001
    )
    assert (session_path / "audio" / "microphone.wav").is_file()


def test_stop_during_preparing_cancels_start_without_beginning_recording(
    tmp_path: Path,
    qapp: QApplication,
) -> None:
    controller = _make_controller(tmp_path, failing_device_ids=set())
    backend = _BlockingOpenAudioBackend()
    controller._audio_backend = backend  # type: ignore[assignment]  # noqa: SLF001
    started_paths: list[str] = []
    cancelled_paths: list[str] = []
    controller.session_started.connect(started_paths.append)
    controller.session_start_cancelled.connect(cancelled_paths.append)

    session_path = controller.start_session(
        title="cancel startup",
        microphone=AudioDevice("slow-mic", "Slow microphone", 1),
        system_audio=None,
        screen_target=None,
    )
    assert backend.open_started.wait(1.0)

    controller.stop_session()
    backend.release_open.set()
    _wait_for(
        lambda: not controller.is_recording and controller._session_log is None  # noqa: SLF001
    )

    metadata = json.loads((session_path / "session.json").read_text(encoding="utf-8"))
    assert metadata["status"] == SessionStatus.FAILED_TO_START
    assert any(warning["code"] == "SESSION_START_CANCELLED" for warning in metadata["warnings"])
    assert started_paths == []
    _wait_for(lambda: bool(cancelled_paths))
    assert cancelled_paths == [str(session_path)]
    events = [
        json.loads(line)
        for line in (session_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert any(event["type"] == "session_start_cancel_requested" for event in events)
    assert controller._session_terminal.is_set()  # noqa: SLF001
    assert events[-1]["type"] == "session_start_cancelled"


def test_session_log_open_failure_prevents_recording_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = _make_controller(tmp_path, failing_device_ids=set())

    def fail_to_open_log(*_args, **_kwargs):
        raise OSError("log directory is unavailable")

    monkeypatch.setattr(
        "summarize_meeting.application.recording_controller.SessionLogWriter",
        fail_to_open_log,
    )

    with pytest.raises(RuntimeError, match="セッションログを作成できない"):
        controller.start_session(
            title="private meeting",
            microphone=AudioDevice("private-mic", "Private microphone", 1),
            system_audio=None,
            screen_target=None,
        )

    meeting_dirs = list((tmp_path / "data" / "meetings").iterdir())
    metadata = json.loads((meeting_dirs[0] / "session.json").read_text(encoding="utf-8"))
    assert metadata["status"] == SessionStatus.FAILED_TO_START
    assert any(warning["code"] == "SESSION_LOG_OPEN_FAILED" for warning in metadata["warnings"])
    assert not controller.is_recording


def test_start_cleanup_manifest_failure_still_reaches_terminal_state(
    tmp_path: Path,
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = _make_controller(tmp_path, failing_device_ids={"mic"})

    def fail_manifest(*_args, **_kwargs) -> None:
        raise OSError("manifest destination is unavailable")

    monkeypatch.setattr(
        RecordingController,
        "_write_audio_manifest",
        staticmethod(fail_manifest),
    )
    failures: list[tuple[str, str]] = []
    controller.session_start_failed.connect(lambda path, message: failures.append((path, message)))

    session_path = controller.start_session(
        title="cleanup manifest failure",
        microphone=AudioDevice("mic", "Broken mic", 1),
        system_audio=None,
        screen_target=None,
    )
    _wait_for(
        lambda: not controller.is_recording and controller._session_log is None  # noqa: SLF001
    )

    metadata = json.loads((session_path / "session.json").read_text(encoding="utf-8"))
    assert metadata["status"] == SessionStatus.FAILED_TO_START
    assert any(
        warning["code"] == "START_CLEANUP_FAILED" and "audio manifest" in warning["message"]
        for warning in metadata["warnings"]
    )
    assert controller._session_terminal.is_set()  # noqa: SLF001
    _wait_for(lambda: bool(failures))
