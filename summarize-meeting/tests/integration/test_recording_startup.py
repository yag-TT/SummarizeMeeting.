from __future__ import annotations

import json
import time
from collections import deque
from pathlib import Path

import numpy as np
import pytest

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
        time.sleep(0.01)
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
    _wait_for(lambda: not controller.is_recording)

    metadata = json.loads((session_path / "session.json").read_text(encoding="utf-8"))
    assert metadata["status"] == SessionStatus.RECORDED
    assert (session_path / "audio" / "system.wav").is_file()


def test_session_is_failed_to_start_when_all_audio_sources_fail(
    tmp_path: Path,
) -> None:
    controller = _make_controller(tmp_path, failing_device_ids={"mic", "system"})

    with pytest.raises(RuntimeError, match="どちらも開始できません"):
        controller.start_session(
            title="no audio",
            microphone=AudioDevice("mic", "Broken mic", 1),
            system_audio=AudioDevice("system", "Broken output", 1),
            screen_target=None,
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
    assert not controller.is_recording
