from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np

from summarize_meeting.application.recording_controller import RecordingController
from summarize_meeting.capture.audio.recorder import AudioTrackRecorder
from summarize_meeting.domain.capture import AudioDevice, AudioFormat
from summarize_meeting.domain.session import (
    ComponentKind,
    ComponentStatus,
    RecordingSession,
    SessionStatus,
)
from summarize_meeting.infrastructure.paths import PortableAppPaths
from summarize_meeting.infrastructure.session_repository import FileSessionRepository


class _AudioStream:
    audio_format = AudioFormat(sample_rate=8_000, channels=1)

    def read(self, frames: int) -> np.ndarray:
        time.sleep(0.005)
        return np.full((frames, 1), 0.1, dtype=np.float32)

    def close(self) -> None:
        pass


class _AudioBackend:
    def open_stream(self, _device_id: str, *, sample_rate: int, block_frames: int):
        return _AudioStream()


class _AppendFailingRepository:
    def __init__(self, delegate: FileSessionRepository) -> None:
        self._delegate = delegate

    def save(self, paths, session) -> None:
        self._delegate.save(paths, session)

    def append_event(self, _paths, _event) -> None:
        raise OSError("events file is temporarily unavailable")


def _wait_for(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert predicate()


def test_audio_capture_continues_when_session_metadata_event_write_fails(
    tmp_path: Path,
) -> None:
    app_paths = PortableAppPaths(tmp_path)
    app_paths.ensure_writable()
    controller = RecordingController(app_paths)
    session = RecordingSession(title="metadata failure", status=SessionStatus.RECORDING)
    session.audio[ComponentKind.MICROPHONE.value] = {"id": "mic"}
    session.set_component(ComponentKind.SESSION_STORAGE, ComponentStatus.RUNNING)
    paths = controller._repository.create(session)  # noqa: SLF001
    controller._session = session  # noqa: SLF001
    controller._session_paths = paths  # noqa: SLF001
    controller._origin_ns = time.perf_counter_ns()  # noqa: SLF001
    controller._repository = _AppendFailingRepository(  # type: ignore[assignment]  # noqa: SLF001
        controller._repository  # noqa: SLF001
    )
    errors: list[str] = []
    controller.fatal_error.connect(errors.append)
    recorder = AudioTrackRecorder(
        backend=_AudioBackend(),  # type: ignore[arg-type]
        device=AudioDevice("mic", "Conference mic", 1),
        track_name="microphone",
        audio_dir=paths.audio,
        state_callback=lambda status, code, message: controller._set_component(  # noqa: SLF001
            ComponentKind.MICROPHONE,
            status,
            code,
            message,
        ),
        meter_callback=lambda _level: None,
        block_frames=800,
        origin_ns=controller._origin_ns,  # noqa: SLF001
    )

    recorder.start()
    _wait_for(
        lambda: session.components[ComponentKind.MICROPHONE.value].status == ComponentStatus.RUNNING
    )
    time.sleep(0.03)
    stats = recorder.finish()

    assert stats is not None
    assert stats.frames_written > 0
    assert stats.validated
    assert (paths.audio / "microphone.wav").is_file()
    assert session.components[ComponentKind.MICROPHONE.value].status == ComponentStatus.STOPPED
    assert session.components[ComponentKind.SESSION_STORAGE.value].status == (
        ComponentStatus.FAILED
    )
    assert len(errors) == 1
    assert "音声録音を可能な限り継続" in errors[0]
    metadata = json.loads(paths.session_json.read_text(encoding="utf-8"))
    warnings = [
        warning
        for warning in metadata["warnings"]
        if warning["code"] == "SESSION_METADATA_WRITE_FAILED"
    ]
    assert len(warnings) == 1
