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


class DisconnectingAudioStream(FakeAudioStream):
    def __init__(self) -> None:
        self._reads = 0

    def read(self, frames: int) -> np.ndarray:
        self._reads += 1
        if self._reads >= 2:
            raise RuntimeError("device disconnected")
        return super().read(frames)


class ReconnectingAudioBackend(FakeAudioBackend):
    def __init__(self, *, reconnect_succeeds: bool) -> None:
        self.reconnect_succeeds = reconnect_succeeds
        self.opened_device_ids: list[str] = []

    def open_stream(
        self,
        device_id: str,
        *,
        sample_rate: int,
        block_frames: int,
    ) -> FakeAudioStream:
        self.opened_device_ids.append(device_id)
        if len(self.opened_device_ids) == 1:
            return DisconnectingAudioStream()
        if not self.reconnect_succeeds:
            raise RuntimeError("device is still unavailable")
        return FakeAudioStream()


def _wait_for(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert predicate()


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
    _wait_for(lambda: ComponentStatus.RUNNING in states)
    time.sleep(0.03)
    stats = recorder.finish()

    assert stats is not None
    assert ComponentStatus.RUNNING in states
    assert states[-1] == ComponentStatus.STOPPED
    assert meters
    with wave.open(str(audio_dir / "microphone.wav"), "rb") as stream:
        assert stream.getnframes() > 0


def test_audio_recorder_reconnects_only_the_same_device(tmp_path: Path) -> None:
    states: list[ComponentStatus] = []
    backend = ReconnectingAudioBackend(reconnect_succeeds=True)
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    recorder = AudioTrackRecorder(
        backend=backend,
        device=AudioDevice("selected-device", "Selected", 2),
        track_name="system",
        audio_dir=audio_dir,
        state_callback=lambda state, _code, _message: states.append(state),
        meter_callback=lambda _level: None,
        block_frames=800,
        reconnect_interval_seconds=0.01,
    )

    recorder.start()
    _wait_for(lambda: states.count(ComponentStatus.RUNNING) >= 2)
    time.sleep(0.03)
    stats = recorder.finish()

    assert stats is not None
    assert ComponentStatus.RECONNECTING in states
    assert set(backend.opened_device_ids) == {"selected-device"}
    assert len(stats.gaps) == 1
    assert stats.gaps[0].outcome == "reconnected"
    assert stats.segments >= 2


def test_audio_recorder_marks_failed_reconnect_in_gap(tmp_path: Path) -> None:
    states: list[ComponentStatus] = []
    backend = ReconnectingAudioBackend(reconnect_succeeds=False)
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    recorder = AudioTrackRecorder(
        backend=backend,
        device=AudioDevice("selected-device", "Selected", 2),
        track_name="microphone",
        audio_dir=audio_dir,
        state_callback=lambda state, _code, _message: states.append(state),
        meter_callback=lambda _level: None,
        block_frames=800,
        reconnect_attempts=2,
        reconnect_interval_seconds=0.01,
    )

    recorder.start()
    _wait_for(lambda: ComponentStatus.FAILED in states)
    stats = recorder.finish()

    assert stats is not None
    assert states[-1] == ComponentStatus.FAILED
    assert stats.gaps[0].outcome == "failed"
    assert stats.gaps[0].reconnect_attempts == 2


def test_stop_interrupts_reconnect_wait(tmp_path: Path) -> None:
    states: list[ComponentStatus] = []
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    recorder = AudioTrackRecorder(
        backend=ReconnectingAudioBackend(reconnect_succeeds=False),
        device=AudioDevice("selected-device", "Selected", 2),
        track_name="microphone",
        audio_dir=audio_dir,
        state_callback=lambda state, _code, _message: states.append(state),
        meter_callback=lambda _level: None,
        block_frames=800,
        reconnect_attempts=5,
        reconnect_interval_seconds=5.0,
    )

    recorder.start()
    _wait_for(lambda: ComponentStatus.RECONNECTING in states)
    started = time.monotonic()
    stats = recorder.finish(timeout=2.0)

    assert time.monotonic() - started < 1.0
    assert stats is not None
    assert stats.gaps[0].outcome == "stopped"
    assert states[-1] == ComponentStatus.STOPPED
