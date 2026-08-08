from __future__ import annotations

import json
import time
from collections import deque
from pathlib import Path

import pytest

from summarize_meeting.application.recording_controller import RecordingController
from summarize_meeting.application.storage_monitor import (
    InsufficientDiskSpaceError,
    StorageCapacity,
    StorageMonitor,
)
from summarize_meeting.domain.capture import AudioDevice
from summarize_meeting.domain.session import (
    ComponentKind,
    ComponentStatus,
    RecordingSession,
    SessionStatus,
)
from summarize_meeting.infrastructure.paths import PortableAppPaths


class _SequenceProbe:
    def __init__(self, *values: int) -> None:
        self._values = deque(values)

    def free_bytes(self, path: Path) -> int:
        return self._values.popleft()


class _FakeScreenRecorder:
    def __init__(self, controller: RecordingController) -> None:
        self._controller = controller
        self.failures: list[tuple[str, str]] = []

    def fail(self, error_code: str, message: str) -> None:
        self.failures.append((error_code, message))
        self._controller._set_component(  # noqa: SLF001
            ComponentKind.SCREEN,
            ComponentStatus.FAILED,
            error_code,
            message,
        )


class _FakeAudioRecorder:
    def __init__(self) -> None:
        self.stop_requested = False

    def request_stop(self) -> None:
        self.stop_requested = True


def _make_controller(
    tmp_path: Path,
    *,
    free_bytes: int,
    minimum_free_bytes: int = 100,
) -> RecordingController:
    paths = PortableAppPaths(tmp_path)
    paths.ensure_writable()
    monitor = StorageMonitor(
        path=paths.meetings_dir,
        probe=_SequenceProbe(free_bytes),
        minimum_free_bytes=minimum_free_bytes,
    )
    return RecordingController(paths, storage_monitor=monitor)


def test_controller_rejects_start_before_creating_session_when_space_is_low(
    tmp_path: Path,
) -> None:
    controller = _make_controller(tmp_path, free_bytes=99)

    with pytest.raises(InsufficientDiskSpaceError):
        controller.start_session(
            title="low disk",
            microphone=AudioDevice(id="mic", name="Fake mic", channels=1),
            system_audio=None,
            screen_target=None,
        )

    assert list((tmp_path / "data" / "meetings").iterdir()) == []


def test_controller_stops_only_screen_and_records_low_space_event(tmp_path: Path) -> None:
    controller = _make_controller(tmp_path, free_bytes=100)
    session = RecordingSession(title="recording", status=SessionStatus.RECORDING)
    session.audio[ComponentKind.MICROPHONE.value] = {"id": "mic"}
    session.set_component(ComponentKind.MICROPHONE, ComponentStatus.RUNNING)
    paths = controller._repository.create(session)  # noqa: SLF001
    controller._session = session  # noqa: SLF001
    controller._session_paths = paths  # noqa: SLF001
    controller._origin_ns = time.perf_counter_ns()  # noqa: SLF001
    screen = _FakeScreenRecorder(controller)
    controller._screen_recorder = screen  # type: ignore[assignment]  # noqa: SLF001
    audio = _FakeAudioRecorder()
    controller._audio_recorders = {  # type: ignore[dict-item]  # noqa: SLF001
        ComponentKind.MICROPHONE: audio
    }
    messages: list[str] = []
    controller.fatal_error.connect(messages.append)

    controller._on_low_disk_space(  # noqa: SLF001
        StorageCapacity(free_bytes=50, minimum_free_bytes=100)
    )

    assert session.components[ComponentKind.MICROPHONE.value].status == ComponentStatus.RUNNING
    assert session.components[ComponentKind.SCREEN.value].status == ComponentStatus.FAILED
    assert session.components[ComponentKind.SESSION_STORAGE.value].status == ComponentStatus.FAILED
    assert screen.failures[0][0] == "LOW_DISK_SPACE"
    assert not audio.stop_requested
    assert messages and "音声録音を継続" in messages[0]
    events = [
        json.loads(line)
        for line in paths.events.read_text(encoding="utf-8").splitlines()
    ]
    low_space = next(event for event in events if event["type"] == "low_disk_space")
    assert low_space["free_bytes"] == 50
    assert low_space["minimum_free_bytes"] == 100
