from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np

from summarize_meeting.capture.audio.base import AudioBackend, FloatAudio
from summarize_meeting.capture.audio.meter import normalized_rms
from summarize_meeting.domain.capture import AudioDevice, AudioFormat
from summarize_meeting.domain.session import ComponentStatus
from summarize_meeting.infrastructure.audio_writer import (
    AudioGap,
    AudioTrackStats,
    ProgressCallback,
    SegmentedWaveWriter,
)

StateCallback = Callable[[ComponentStatus, str | None, str | None], None]
MeterCallback = Callable[[float], None]
ExceptionCallback = Callable[[str, Exception], None]
WriterFactory = Callable[[Path, str, AudioFormat], SegmentedWaveWriter]


@dataclass(frozen=True, slots=True)
class _AudioChunk:
    samples: FloatAudio
    captured_at_ns: int


class _RotateSegment:
    pass


_ROTATE_SEGMENT = _RotateSegment()


class AudioQueueFullError(RuntimeError):
    pass


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
        origin_ns: int | None = None,
        reconnect_attempts: int = 5,
        reconnect_interval_seconds: float = 2.0,
        writer_factory: WriterFactory = SegmentedWaveWriter,
        queue_pressure_ratio: float = 0.8,
        queue_recovery_ratio: float = 0.5,
        queue_put_timeout_seconds: float = 1.0,
        start_gate: threading.Event | None = None,
        exception_callback: ExceptionCallback | None = None,
        finalize_progress_callback: ProgressCallback | None = None,
    ) -> None:
        self._backend = backend
        self._device = device
        self._track_name = track_name
        self._audio_dir = audio_dir
        self._state_callback = state_callback
        self._meter_callback = meter_callback
        self._exception_callback = exception_callback
        self._finalize_progress_callback = finalize_progress_callback
        self._sample_rate = sample_rate
        self._block_frames = block_frames
        self._origin_ns = origin_ns
        self._reconnect_attempts = reconnect_attempts
        self._reconnect_interval_seconds = reconnect_interval_seconds
        self._writer_factory = writer_factory
        if not 0.0 < queue_recovery_ratio < queue_pressure_ratio <= 1.0:
            raise ValueError("queue ratios must satisfy 0 < recovery < pressure <= 1")
        self._queue_pressure_ratio = queue_pressure_ratio
        self._queue_recovery_ratio = queue_recovery_ratio
        if queue_put_timeout_seconds <= 0:
            raise ValueError("queue_put_timeout_seconds must be positive")
        self._queue_put_timeout_seconds = queue_put_timeout_seconds
        if queue_seconds < 0:
            raise ValueError("queue_seconds must not be negative")
        self._queue_seconds = queue_seconds
        capacity = self._queue_capacity(sample_rate)
        self._queue: queue.Queue[_AudioChunk | _RotateSegment | None] = queue.Queue(
            maxsize=capacity
        )
        self._stop = threading.Event()
        self._capture_thread: threading.Thread | None = None
        self._writer_thread: threading.Thread | None = None
        self._writer: SegmentedWaveWriter | None = None
        self._writer_error: Exception | None = None
        self._capture_error: Exception | None = None
        self._failed = threading.Event()
        self._last_meter_time = 0.0
        self._gaps: list[AudioGap] = []
        self._estimated_start_offset_ms: int | None = None
        self._capture_ended_offset_ms: int | None = None
        self._overflow_count = 0
        self._queue_pressure_count = 0
        self._max_queue_usage_ratio = 0.0
        self._queue_pressure_active = False
        self._start_gate = start_gate or threading.Event()
        if start_gate is None:
            self._start_gate.set()
        self._ready = threading.Event()
        self._startup_complete = threading.Event()
        self._startup_cancelled = threading.Event()
        self._startup_lock = threading.Lock()

    def start(self) -> None:
        if self._origin_ns is None:
            self._origin_ns = time.perf_counter_ns()
        self._state_callback(ComponentStatus.STARTING, None, None)
        self._capture_thread = threading.Thread(
            target=self._capture_loop,
            name=f"{self._track_name}-capture",
            daemon=True,
        )
        self._capture_thread.start()

    def request_stop(self) -> None:
        self._stop.set()

    @property
    def is_ready(self) -> bool:
        return self._ready.is_set()

    @property
    def startup_complete(self) -> bool:
        return self._startup_complete.is_set()

    def wait_until_initialized(self, timeout: float) -> bool:
        self._startup_complete.wait(timeout=max(0.0, timeout))
        return self._ready.is_set()

    def cancel_start(self, error_code: str, message: str) -> None:
        self._startup_cancelled.set()
        self._stop.set()
        error = TimeoutError(message)
        if self._complete_startup_failure(error_code, message, error):
            self._notify_exception(error_code, error)

    def finish(self, timeout: float = 15.0) -> AudioTrackStats | None:
        if not self._failed.is_set():
            self._state_callback(ComponentStatus.STOPPING, None, None)
        self.request_stop()
        self._report_finalize_progress("stopping_capture", 0, 1)
        if self._capture_thread is not None:
            self._capture_thread.join(timeout=timeout)
        if self._capture_thread is not None and self._capture_thread.is_alive():
            raise TimeoutError(f"{self._track_name} capture did not stop")
        self._report_finalize_progress("stopping_capture", 1, 1)
        self._report_finalize_progress("draining", 0, 1)
        if self._writer_thread is not None:
            self._writer_thread.join(timeout=timeout)
        if self._writer_thread is not None and self._writer_thread.is_alive():
            raise TimeoutError(f"{self._track_name} writer did not stop")
        self._report_finalize_progress("draining", 1, 1)
        if self._writer_error is not None:
            raise self._writer_error
        if self._writer is None:
            return None
        stats = self._with_capture_diagnostics(self._writer.close())
        if self._capture_error is None:
            self._state_callback(ComponentStatus.STOPPED, None, None)
        return stats

    def _capture_loop(self) -> None:
        stream = None
        try:
            stream = self._open_with_fallback()
            self._queue = queue.Queue(maxsize=self._queue_capacity(stream.audio_format.sample_rate))
            self._writer = self._writer_factory(
                self._audio_dir,
                self._track_name,
                stream.audio_format,
            )
            self._writer.set_progress_callback(self._finalize_progress_callback)
            self._writer_thread = threading.Thread(
                target=self._writer_loop,
                name=f"{self._track_name}-writer",
                daemon=True,
            )
            self._writer_thread.start()
            if not self._complete_startup_ready():
                return
            while not self._start_gate.wait(timeout=0.1):
                if self._stop.is_set():
                    return
            if self._stop.is_set():
                return
            self._state_callback(ComponentStatus.RUNNING, None, None)
            while not self._stop.is_set():
                try:
                    samples = stream.read(self._block_frames)
                except Exception as exc:
                    self._close_stream(stream)
                    stream = self._reconnect(exc)
                    if stream is None:
                        break
                    self._enqueue(_ROTATE_SEGMENT)
                    self._state_callback(ComponentStatus.RUNNING, None, "再接続しました")
                    continue
                if samples.size == 0:
                    continue
                if self._writer_error is not None:
                    break
                captured_at_ns = time.perf_counter_ns()
                self._record_first_chunk(samples, captured_at_ns)
                now = time.monotonic()
                if now - self._last_meter_time >= 0.1:
                    self._last_meter_time = now
                    self._meter_callback(normalized_rms(samples))
                self._enqueue(_AudioChunk(samples.copy(), captured_at_ns))
        except AudioQueueFullError as exc:
            self._capture_error = exc
            self._failed.set()
            self._state_callback(ComponentStatus.FAILED, "AUDIO_QUEUE_PRESSURE", str(exc))
            self._notify_exception("AUDIO_QUEUE_PRESSURE", exc)
        except Exception as exc:
            if self._startup_cancelled.is_set():
                pass
            elif self._complete_startup_failure("AUDIO_OPEN_FAILED", str(exc), exc):
                self._notify_exception("AUDIO_OPEN_FAILED", exc)
            else:
                self._capture_error = exc
                self._failed.set()
                self._state_callback(ComponentStatus.FAILED, "AUDIO_CAPTURE_FAILED", str(exc))
                self._notify_exception("AUDIO_CAPTURE_FAILED", exc)
        finally:
            self._startup_complete.set()
            self._capture_ended_offset_ms = self._timestamp_ms()
            if stream is not None:
                self._close_stream(stream)
            self._enqueue_sentinel()

    def _complete_startup_ready(self) -> bool:
        with self._startup_lock:
            if self._startup_complete.is_set() or self._stop.is_set():
                return False
            self._state_callback(ComponentStatus.READY, None, None)
            self._ready.set()
            self._startup_complete.set()
            return True

    def _complete_startup_failure(
        self,
        error_code: str,
        message: str,
        error: Exception,
    ) -> bool:
        with self._startup_lock:
            if self._startup_complete.is_set():
                return False
            self._capture_error = error
            self._failed.set()
            self._state_callback(ComponentStatus.FAILED, error_code, message)
            self._startup_complete.set()
            return True

    def _reconnect(self, cause: Exception):
        assert self._writer is not None
        gap_start_ms = self._timestamp_ms()
        self._notify_exception("AUDIO_DEVICE_DISCONNECTED", cause)
        self._state_callback(
            ComponentStatus.RECONNECTING,
            "AUDIO_DEVICE_DISCONNECTED",
            str(cause),
        )
        last_error = cause
        attempts = 0
        for attempt in range(1, self._reconnect_attempts + 1):
            if attempt > 1 and self._stop.wait(self._reconnect_interval_seconds):
                self._append_gap(gap_start_ms, attempts, "stopped")
                return None
            if self._stop.is_set():
                self._append_gap(gap_start_ms, attempts, "stopped")
                return None
            attempts = attempt
            try:
                replacement = self._backend.open_stream(
                    self._device.id,
                    sample_rate=self._writer.audio_format.sample_rate,
                    block_frames=self._block_frames,
                )
                if replacement.audio_format != self._writer.audio_format:
                    actual_format = replacement.audio_format
                    self._close_stream(replacement)
                    raise RuntimeError(
                        "再接続後の音声形式が変わりました: "
                        f"expected={self._writer.audio_format} actual={actual_format}"
                    )
                self._append_gap(gap_start_ms, attempts, "reconnected")
                return replacement
            except Exception as exc:
                last_error = exc

        self._append_gap(gap_start_ms, attempts, "failed")
        self._capture_error = last_error
        self._failed.set()
        self._state_callback(ComponentStatus.FAILED, "AUDIO_RECONNECT_FAILED", str(last_error))
        self._notify_exception("AUDIO_RECONNECT_FAILED", last_error)
        return None

    def _append_gap(self, start_ms: int, attempts: int, outcome: str) -> None:
        self._gaps.append(
            AudioGap(
                start_ms=start_ms,
                end_ms=self._timestamp_ms(),
                reconnect_attempts=attempts,
                outcome=outcome,
            )
        )

    def _timestamp_ms(self) -> int:
        assert self._origin_ns is not None
        return int((time.perf_counter_ns() - self._origin_ns) // 1_000_000)

    @staticmethod
    def _close_stream(stream) -> None:
        with suppress(Exception):
            stream.close()

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
                item = self._queue.get()
                if item is None:
                    break
                if item is _ROTATE_SEGMENT:
                    self._writer.rotate_segment()
                    continue
                self._writer.write(np.asarray(item.samples, dtype=np.float32))
        except Exception as exc:
            self._writer_error = exc
            self._failed.set()
            self._stop.set()
            self._state_callback(ComponentStatus.FAILED, "AUDIO_WRITE_FAILED", str(exc))
            self._notify_exception("AUDIO_WRITE_FAILED", exc)

    def _notify_exception(self, error_code: str, exception: Exception) -> None:
        if self._exception_callback is None:
            return
        with suppress(Exception):
            self._exception_callback(error_code, exception)

    def _report_finalize_progress(
        self,
        phase: str,
        completed: int,
        total: int,
    ) -> None:
        if self._finalize_progress_callback is None:
            return
        with suppress(Exception):
            self._finalize_progress_callback(phase, completed, total)

    def _enqueue_sentinel(self) -> None:
        while True:
            try:
                self._queue.put(None, timeout=0.1)
                return
            except queue.Full:
                if self._writer_thread is None or not self._writer_thread.is_alive():
                    return

    def _enqueue(self, item: _AudioChunk | _RotateSegment) -> None:
        try:
            self._queue.put(item, timeout=self._queue_put_timeout_seconds)
        except queue.Full as exc:
            self._overflow_count += 1
            raise AudioQueueFullError("音声書込みqueueが満杯になりました") from exc
        usage_ratio = self._queue.qsize() / self._queue.maxsize
        self._max_queue_usage_ratio = max(self._max_queue_usage_ratio, usage_ratio)
        if (
            usage_ratio >= self._queue_pressure_ratio
            and not self._queue_pressure_active
            and not self._stop.is_set()
        ):
            self._queue_pressure_active = True
            self._queue_pressure_count += 1
            self._state_callback(
                ComponentStatus.RUNNING,
                "AUDIO_QUEUE_PRESSURE",
                f"音声書込みqueue使用率が {usage_ratio:.0%} です",
            )
        elif usage_ratio <= self._queue_recovery_ratio:
            self._queue_pressure_active = False

    def _queue_capacity(self, sample_rate: int) -> int:
        return max(2, int(self._queue_seconds * sample_rate // self._block_frames))

    def _record_first_chunk(self, samples: FloatAudio, captured_at_ns: int) -> None:
        if self._estimated_start_offset_ms is not None:
            return
        assert self._origin_ns is not None
        assert self._writer is not None
        chunk_duration_ns = round(
            samples.shape[0] * 1_000_000_000 / self._writer.audio_format.sample_rate
        )
        estimated_ns = captured_at_ns - self._origin_ns - chunk_duration_ns
        self._estimated_start_offset_ms = max(0, round(estimated_ns / 1_000_000))

    def _with_capture_diagnostics(self, stats: AudioTrackStats) -> AudioTrackStats:
        active_duration_ms: float | None = None
        drift_ms: float | None = None
        if (
            self._estimated_start_offset_ms is not None
            and self._capture_ended_offset_ms is not None
        ):
            gap_duration_ms = sum(max(0, gap.end_ms - gap.start_ms) for gap in self._gaps)
            active_duration_ms = max(
                0.0,
                float(
                    self._capture_ended_offset_ms
                    - self._estimated_start_offset_ms
                    - gap_duration_ms
                ),
            )
            drift_ms = round(stats.audio_duration_ms - active_duration_ms, 3)
        return replace(
            stats,
            estimated_start_offset_ms=self._estimated_start_offset_ms,
            capture_ended_offset_ms=self._capture_ended_offset_ms,
            active_capture_duration_ms=active_duration_ms,
            duration_drift_ms=drift_ms,
            overflow_count=self._overflow_count,
            queue_pressure_count=self._queue_pressure_count,
            max_queue_usage_ratio=round(self._max_queue_usage_ratio, 6),
            queue_capacity_chunks=self._queue.maxsize,
            gaps=tuple(self._gaps),
        )
