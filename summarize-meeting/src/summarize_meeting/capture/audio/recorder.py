from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np

from summarize_meeting.capture.audio.base import AudioBackend, FloatAudio
from summarize_meeting.capture.audio.meter import normalized_rms
from summarize_meeting.domain.capture import AudioDevice
from summarize_meeting.domain.session import ComponentStatus
from summarize_meeting.infrastructure.audio_writer import AudioTrackStats, SegmentedWaveWriter

StateCallback = Callable[[ComponentStatus, str | None, str | None], None]
MeterCallback = Callable[[float], None]


class AudioTrackRecorder:
    def __init__(
        self,
        *,
        backend: AudioBackend,
        device: AudioDevice,
        track_name: str,
        audio_dir: Path,
        state_callback: StateCallback,
        meter_callback: MeterCallback,
        sample_rate: int = 48_000,
        block_frames: int = 4_800,
        queue_seconds: int = 30,
    ) -> None:
        self._backend = backend
        self._device = device
        self._track_name = track_name
        self._audio_dir = audio_dir
        self._state_callback = state_callback
        self._meter_callback = meter_callback
        self._sample_rate = sample_rate
        self._block_frames = block_frames
        capacity = max(2, queue_seconds * sample_rate // block_frames)
        self._queue: queue.Queue[FloatAudio | None] = queue.Queue(maxsize=capacity)
        self._stop = threading.Event()
        self._capture_thread: threading.Thread | None = None
        self._writer_thread: threading.Thread | None = None
        self._writer: SegmentedWaveWriter | None = None
        self._writer_error: Exception | None = None
        self._capture_error: Exception | None = None
        self._failed = threading.Event()
        self._last_meter_time = 0.0

    def start(self) -> None:
        self._state_callback(ComponentStatus.STARTING, None, None)
        self._capture_thread = threading.Thread(
            target=self._capture_loop,
            name=f"{self._track_name}-capture",
            daemon=True,
        )
        self._capture_thread.start()

    def request_stop(self) -> None:
        self._stop.set()

    def finish(self, timeout: float = 15.0) -> AudioTrackStats | None:
        if not self._failed.is_set():
            self._state_callback(ComponentStatus.STOPPING, None, None)
        self.request_stop()
        if self._capture_thread is not None:
            self._capture_thread.join(timeout=timeout)
        if self._capture_thread is not None and self._capture_thread.is_alive():
            raise TimeoutError(f"{self._track_name} capture did not stop")
        if self._writer_thread is not None:
            self._writer_thread.join(timeout=timeout)
        if self._writer_thread is not None and self._writer_thread.is_alive():
            raise TimeoutError(f"{self._track_name} writer did not stop")
        if self._writer_error is not None:
            raise self._writer_error
        if self._writer is None:
            return None
        stats = self._writer.close()
        if self._capture_error is None:
            self._state_callback(ComponentStatus.STOPPED, None, None)
        return stats

    def _capture_loop(self) -> None:
        stream = None
        try:
            stream = self._open_with_fallback()
            self._writer = SegmentedWaveWriter(
                self._audio_dir,
                self._track_name,
                stream.audio_format,
            )
            self._writer_thread = threading.Thread(
                target=self._writer_loop,
                name=f"{self._track_name}-writer",
                daemon=True,
            )
            self._writer_thread.start()
            self._state_callback(ComponentStatus.RUNNING, None, None)
            while not self._stop.is_set():
                samples = stream.read(self._block_frames)
                if samples.size == 0:
                    continue
                now = time.monotonic()
                if now - self._last_meter_time >= 0.1:
                    self._last_meter_time = now
                    self._meter_callback(normalized_rms(samples))
                try:
                    self._queue.put(samples.copy(), timeout=1.0)
                except queue.Full as exc:
                    raise RuntimeError("AUDIO_QUEUE_PRESSURE") from exc
        except Exception as exc:
            self._capture_error = exc
            self._failed.set()
            self._state_callback(ComponentStatus.FAILED, "AUDIO_CAPTURE_FAILED", str(exc))
        finally:
            if stream is not None:
                stream.close()
            self._enqueue_sentinel()

    def _open_with_fallback(self):
        last_error: Exception | None = None
        for sample_rate in (self._sample_rate, 44_100):
            try:
                return self._backend.open_stream(
                    self._device.id,
                    sample_rate=sample_rate,
                    block_frames=self._block_frames,
                )
            except Exception as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    def _writer_loop(self) -> None:
        assert self._writer is not None
        try:
            while True:
                samples = self._queue.get()
                if samples is None:
                    break
                self._writer.write(np.asarray(samples, dtype=np.float32))
        except Exception as exc:
            self._writer_error = exc
            self._failed.set()
            self._stop.set()
            self._state_callback(ComponentStatus.FAILED, "AUDIO_WRITE_FAILED", str(exc))

    def _enqueue_sentinel(self) -> None:
        while True:
            try:
                self._queue.put(None, timeout=0.1)
                return
            except queue.Full:
                if self._writer_thread is None or not self._writer_thread.is_alive():
                    return
