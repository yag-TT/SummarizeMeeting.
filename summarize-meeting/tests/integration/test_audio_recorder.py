from __future__ import annotations

import threading
import time
import wave
from pathlib import Path

import numpy as np
import pytest

from summarize_meeting.capture.audio.recorder import AudioTrackRecorder
from summarize_meeting.domain.capture import AudioDevice, AudioFormat
from summarize_meeting.domain.session import ComponentStatus
from summarize_meeting.infrastructure.audio_writer import SegmentedWaveWriter


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


class OffsetAudioStream:
    audio_format = AudioFormat(sample_rate=1_000, channels=1)

    def __init__(self, initial_delay_seconds: float) -> None:
        self._initial_delay_seconds = initial_delay_seconds
        self._first_read = True

    def read(self, frames: int) -> np.ndarray:
        delay = frames / self.audio_format.sample_rate
        if self._first_read:
            delay += self._initial_delay_seconds
            self._first_read = False
        time.sleep(delay)
        return np.full((frames, 1), 0.1, dtype=np.float32)

    def close(self) -> None:
        pass


class OffsetAudioBackend(FakeAudioBackend):
    def __init__(self, initial_delay_seconds: float) -> None:
        self._initial_delay_seconds = initial_delay_seconds

    def open_stream(
        self,
        device_id: str,
        *,
        sample_rate: int,
        block_frames: int,
    ) -> OffsetAudioStream:
        return OffsetAudioStream(self._initial_delay_seconds)


class BurstAudioStream(FakeAudioStream):
    def read(self, frames: int) -> np.ndarray:
        return np.full((frames, 2), 0.1, dtype=np.float32)


class BurstAudioBackend(FakeAudioBackend):
    def open_stream(
        self,
        device_id: str,
        *,
        sample_rate: int,
        block_frames: int,
    ) -> BurstAudioStream:
        return BurstAudioStream()


class SlowWaveWriter(SegmentedWaveWriter):
    def write(self, samples) -> None:
        time.sleep(0.02)
        super().write(samples)


class _ThreadProbe:
    def __init__(self, *, alive: bool) -> None:
        self._alive = alive
        self.join_timeout: float | None = None

    def join(self, timeout: float | None = None) -> None:
        self.join_timeout = timeout

    def is_alive(self) -> bool:
        return self._alive


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
    assert stats.estimated_start_offset_ms is not None
    assert stats.capture_ended_offset_ms is not None
    assert stats.active_capture_duration_ms is not None
    assert stats.duration_drift_ms is not None
    assert stats.overflow_count == 0
    assert stats.queue_capacity_chunks == 300
    with wave.open(str(audio_dir / "microphone.wav"), "rb") as stream:
        assert stream.getnframes() > 0


def test_audio_recorder_uses_separate_stop_timeouts_and_keeps_segments_on_drain_timeout(
    tmp_path: Path,
) -> None:
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    recorder = AudioTrackRecorder(
        backend=FakeAudioBackend(),
        device=AudioDevice("fake", "Fake", 2),
        track_name="microphone",
        audio_dir=audio_dir,
        state_callback=lambda _state, _code, _message: None,
        meter_callback=lambda _level: None,
    )
    writer = SegmentedWaveWriter(
        audio_dir,
        "microphone",
        AudioFormat(sample_rate=100, channels=1),
    )
    writer.write(np.full((20, 1), 0.1, dtype=np.float32))
    capture_thread = _ThreadProbe(alive=False)
    writer_thread = _ThreadProbe(alive=True)
    recorder._capture_thread = capture_thread  # type: ignore[assignment]  # noqa: SLF001
    recorder._writer_thread = writer_thread  # type: ignore[assignment]  # noqa: SLF001
    recorder._writer = writer  # noqa: SLF001

    with pytest.raises(TimeoutError, match="writer queue did not drain within 30s"):
        recorder.finish()

    assert capture_thread.join_timeout == 5.0
    assert writer_thread.join_timeout == 30.0
    assert (audio_dir / ".work" / "microphone" / "000000.wav").is_file()
    assert not (audio_dir / "microphone.wav").exists()
    writer.abort()


def test_audio_recorder_waits_at_ready_until_start_gate_is_released(
    tmp_path: Path,
) -> None:
    states: list[ComponentStatus] = []
    meters: list[float] = []
    start_gate = threading.Event()
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
        start_gate=start_gate,
    )

    recorder.start()

    assert recorder.wait_until_initialized(1.0)
    assert recorder.is_ready
    assert states[-1] == ComponentStatus.READY
    time.sleep(0.03)
    assert ComponentStatus.RUNNING not in states
    assert meters == []

    start_gate.set()
    _wait_for(lambda: ComponentStatus.RUNNING in states)
    stats = recorder.finish()

    assert stats is not None
    assert states[-1] == ComponentStatus.STOPPED


def test_audio_recorder_reconnects_only_the_same_device(tmp_path: Path) -> None:
    states: list[ComponentStatus] = []
    backend = ReconnectingAudioBackend(reconnect_succeeds=True)
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    recorder = AudioTrackRecorder(
        backend=backend,
        device=AudioDevice("selected-device", "Selected", 2),
        track_name="system_audio",
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
    exceptions: list[tuple[str, Exception]] = []
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
        exception_callback=lambda code, exc: exceptions.append((code, exc)),
    )

    recorder.start()
    _wait_for(lambda: ComponentStatus.FAILED in states)
    stats = recorder.finish()

    assert stats is not None
    assert states[-1] == ComponentStatus.FAILED
    assert stats.gaps[0].outcome == "failed"
    assert stats.gaps[0].reconnect_attempts == 2
    assert [code for code, _exc in exceptions] == [
        "AUDIO_DEVICE_DISCONNECTED",
        "AUDIO_RECONNECT_FAILED",
    ]
    assert all(isinstance(exc, RuntimeError) for _code, exc in exceptions)


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


def test_two_tracks_record_estimated_start_offset_from_common_origin(tmp_path: Path) -> None:
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    origin_ns = time.perf_counter_ns()
    microphone = AudioTrackRecorder(
        backend=OffsetAudioBackend(0.0),
        device=AudioDevice("mic", "Mic", 1),
        track_name="microphone",
        audio_dir=audio_dir,
        state_callback=lambda _state, _code, _message: None,
        meter_callback=lambda _level: None,
        block_frames=20,
        origin_ns=origin_ns,
    )
    system = AudioTrackRecorder(
        backend=OffsetAudioBackend(0.08),
        device=AudioDevice("system_audio", "System", 1),
        track_name="system_audio",
        audio_dir=audio_dir,
        state_callback=lambda _state, _code, _message: None,
        meter_callback=lambda _level: None,
        block_frames=20,
        origin_ns=origin_ns,
    )

    microphone.start()
    system.start()
    time.sleep(0.16)
    microphone_stats = microphone.finish()
    system_stats = system.finish()

    assert microphone_stats is not None
    assert system_stats is not None
    assert microphone_stats.estimated_start_offset_ms is not None
    assert system_stats.estimated_start_offset_ms is not None
    assert system_stats.estimated_start_offset_ms - microphone_stats.estimated_start_offset_ms >= 50


def test_queue_pressure_is_reported_without_dropping_chunks(tmp_path: Path) -> None:
    states: list[tuple[ComponentStatus, str | None]] = []
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    recorder = AudioTrackRecorder(
        backend=BurstAudioBackend(),
        device=AudioDevice("fast", "Fast", 2),
        track_name="microphone",
        audio_dir=audio_dir,
        state_callback=lambda state, code, _message: states.append((state, code)),
        meter_callback=lambda _level: None,
        block_frames=800,
        queue_seconds=0,
        writer_factory=SlowWaveWriter,
    )

    recorder.start()
    _wait_for(lambda: any(code == "AUDIO_QUEUE_PRESSURE" for _state, code in states))
    stats = recorder.finish()

    assert stats is not None
    assert stats.queue_capacity_chunks == 2
    assert stats.queue_pressure_count >= 1
    assert stats.max_queue_usage_ratio >= 0.8
    assert stats.overflow_count == 0


def test_full_queue_fails_track_and_records_overflow(tmp_path: Path) -> None:
    states: list[tuple[ComponentStatus, str | None]] = []
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    recorder = AudioTrackRecorder(
        backend=BurstAudioBackend(),
        device=AudioDevice("fast", "Fast", 2),
        track_name="system_audio",
        audio_dir=audio_dir,
        state_callback=lambda state, code, _message: states.append((state, code)),
        meter_callback=lambda _level: None,
        block_frames=800,
        queue_seconds=0,
        writer_factory=SlowWaveWriter,
        queue_put_timeout_seconds=0.001,
    )

    recorder.start()
    _wait_for(lambda: (ComponentStatus.FAILED, "AUDIO_QUEUE_PRESSURE") in states)
    stats = recorder.finish()

    assert stats is not None
    assert stats.overflow_count == 1
    assert states[-1] == (ComponentStatus.FAILED, "AUDIO_QUEUE_PRESSURE")
