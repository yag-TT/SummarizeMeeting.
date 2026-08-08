from __future__ import annotations

import time
import wave
from pathlib import Path

import numpy as np

from summarize_meeting.capture.audio.recorder import AudioTrackRecorder
from summarize_meeting.domain.capture import AudioDevice, AudioFormat
from summarize_meeting.domain.session import ComponentStatus


class FakeAudioStream:
    audio_format = AudioFormat(sample_rate=8_000, channels=2)

    def read(self, frames: int) -> np.ndarray:
        time.sleep(0.005)
        return np.full((frames, 2), 0.1, dtype=np.float32)

    def close(self) -> None:
        pass


class FakeAudioBackend:
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
    ) -> FakeAudioStream:
        return FakeAudioStream()


def test_audio_recorder_writes_fake_capture(tmp_path: Path) -> None:
    states: list[ComponentStatus] = []
    meters: list[float] = []
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    recorder = AudioTrackRecorder(
        backend=FakeAudioBackend(),
        device=AudioDevice("fake", "Fake", 2),
        track_name="microphone",
        audio_dir=audio_dir,
        state_callback=lambda state, _code, _message: states.append(state),
        meter_callback=meters.append,
        block_frames=800,
    )

    recorder.start()
    deadline = time.monotonic() + 2.0
    while ComponentStatus.RUNNING not in states and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.03)
    stats = recorder.finish()

    assert stats is not None
    assert ComponentStatus.RUNNING in states
    assert states[-1] == ComponentStatus.STOPPED
    assert meters
    with wave.open(str(audio_dir / "microphone.wav"), "rb") as stream:
        assert stream.getnframes() > 0
