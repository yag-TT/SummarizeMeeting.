from __future__ import annotations

import json
import os
import shutil
import wave
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
from numpy.typing import ArrayLike

from summarize_meeting.domain.capture import AudioFormat


@dataclass(frozen=True, slots=True)
class AudioGap:
    start_ms: int
    end_ms: int
    reconnect_attempts: int
    outcome: str


@dataclass(frozen=True, slots=True)
class AudioTrackStats:
    file: str
    sample_rate: int
    channels: int
    sample_width_bytes: int
    frames_written: int
    segments: int
    estimated_start_offset_ms: int | None = None
    capture_ended_offset_ms: int | None = None
    audio_duration_ms: float = 0.0
    active_capture_duration_ms: float | None = None
    duration_drift_ms: float | None = None
    overflow_count: int = 0
    queue_pressure_count: int = 0
    max_queue_usage_ratio: float = 0.0
    queue_capacity_chunks: int = 0
    gaps: tuple[AudioGap, ...] = ()
    validated: bool = False
    work_files_removed: bool = False
    work_cleanup_error: str | None = None


@dataclass(frozen=True, slots=True)
class WaveValidation:
    sample_rate: int
    channels: int
    sample_width_bytes: int
    frames: int
    duration_ms: float


class WaveValidationError(RuntimeError):
    pass


def inspect_wave_file(path: Path) -> WaveValidation:
    try:
        with wave.open(str(path), "rb") as stream:
            expected_format = AudioFormat(
                sample_rate=stream.getframerate(),
                channels=stream.getnchannels(),
                sample_width_bytes=stream.getsampwidth(),
            )
            expected_frames = stream.getnframes()
    except (EOFError, OSError, wave.Error) as exc:
        raise WaveValidationError(f"WAV cannot be opened: {path}: {exc}") from exc
    return validate_wave_file(
        path,
        expected_format=expected_format,
        expected_frames=expected_frames,
    )


def validate_wave_file(
    path: Path,
    *,
    expected_format: AudioFormat,
    expected_frames: int,
) -> WaveValidation:
    try:
        with wave.open(str(path), "rb") as stream:
            actual_format = AudioFormat(
                sample_rate=stream.getframerate(),
                channels=stream.getnchannels(),
                sample_width_bytes=stream.getsampwidth(),
            )
            if stream.getcomptype() != "NONE":
                raise WaveValidationError(
                    f"Final WAV is not PCM: {path} ({stream.getcomptype()})"
                )
            if actual_format != expected_format:
                raise WaveValidationError(
                    "Final WAV format mismatch: "
                    f"{path} expected={expected_format} actual={actual_format}"
                )
            declared_frames = stream.getnframes()
            if declared_frames != expected_frames:
                raise WaveValidationError(
                    "Final WAV frame count mismatch: "
                    f"{path} expected={expected_frames} declared={declared_frames}"
                )

            block_align = actual_format.channels * actual_format.sample_width_bytes
            readable_frames = 0
            while content := stream.readframes(65_536):
                if len(content) % block_align != 0:
                    raise WaveValidationError(
                        f"Final WAV contains an incomplete PCM frame: {path}"
                    )
                readable_frames += len(content) // block_align
            if readable_frames != expected_frames:
                raise WaveValidationError(
                    "Final WAV readable frame count mismatch: "
                    f"{path} expected={expected_frames} readable={readable_frames}"
                )
    except WaveValidationError:
        raise
    except (EOFError, OSError, wave.Error) as exc:
        raise WaveValidationError(f"Final WAV cannot be opened: {path}: {exc}") from exc

    return WaveValidation(
        sample_rate=actual_format.sample_rate,
        channels=actual_format.channels,
        sample_width_bytes=actual_format.sample_width_bytes,
        frames=readable_frames,
        duration_ms=round(
            readable_frames * 1000.0 / actual_format.sample_rate,
            3,
        ),
    )


class SegmentedWaveWriter:
    def __init__(
        self,
        audio_dir: Path,
        track_name: str,
        audio_format: AudioFormat,
        *,
        segment_seconds: int = 60,
    ) -> None:
        self._audio_dir = audio_dir
        self._track_name = track_name
        self._format = audio_format
        self._segment_frames = max(1, segment_seconds * audio_format.sample_rate)
        self._work_dir = audio_dir / ".work" / track_name
        self._work_dir.mkdir(parents=True, exist_ok=True)
        self._segments: list[dict[str, int | str]] = []
        self._current: wave.Wave_write | None = None
        self._current_frames = 0
        self._frames_written = 0
        self._closed = False
        self._validated = False
        self._work_files_removed = False
        self._work_cleanup_error: str | None = None

    @property
    def audio_format(self) -> AudioFormat:
        return self._format

    def write(self, samples: ArrayLike) -> None:
        if self._closed:
            raise RuntimeError("Audio writer is closed")
        values = np.asarray(samples, dtype=np.float32)
        if values.ndim == 1:
            values = values[:, np.newaxis]
        if values.ndim != 2 or values.shape[1] != self._format.channels:
            raise ValueError(
                f"Expected frames x {self._format.channels} channels, got {values.shape}"
            )
        pcm = np.rint(np.clip(values, -1.0, 1.0) * 32767.0).astype("<i2")
        offset = 0
        while offset < pcm.shape[0]:
            if self._current is None:
                self._open_segment()
            available = self._segment_frames - self._current_frames
            count = min(available, pcm.shape[0] - offset)
            assert self._current is not None
            self._current.writeframesraw(pcm[offset : offset + count].tobytes())
            self._current_frames += count
            self._frames_written += count
            offset += count
            if self._current_frames >= self._segment_frames:
                self._close_segment()

    def close(self) -> AudioTrackStats:
        if self._closed:
            return self.stats()
        if self._current is not None:
            self._close_segment()
        if not self._segments:
            self._open_segment()
            self._close_segment()
        self._consolidate()
        output = self._audio_dir / f"{self._track_name}.wav"
        validate_wave_file(
            output,
            expected_format=self._format,
            expected_frames=self._frames_written,
        )
        self._validated = True
        self._closed = True
        try:
            self._remove_validated_work_files()
            self._work_files_removed = True
        except OSError as exc:
            self._work_cleanup_error = str(exc)
        return self.stats()

    def abort(self) -> None:
        if self._current is not None:
            self._close_segment()
        self._closed = True

    def rotate_segment(self) -> None:
        if self._closed:
            raise RuntimeError("Audio writer is closed")
        if self._current is not None:
            self._close_segment()

    def stats(self) -> AudioTrackStats:
        return AudioTrackStats(
            file=f"audio/{self._track_name}.wav",
            sample_rate=self._format.sample_rate,
            channels=self._format.channels,
            sample_width_bytes=self._format.sample_width_bytes,
            frames_written=self._frames_written,
            segments=len(self._segments),
            audio_duration_ms=round(
                self._frames_written * 1000.0 / self._format.sample_rate,
                3,
            ),
            validated=self._validated,
            work_files_removed=self._work_files_removed,
            work_cleanup_error=self._work_cleanup_error,
        )

    def _open_segment(self) -> None:
        index = len(self._segments)
        filename = f"{index:06d}.wav"
        stream = wave.open(  # noqa: SIM115 - kept open until the segment rotates
            str(self._work_dir / filename), "wb"
        )
        stream.setnchannels(self._format.channels)
        stream.setsampwidth(self._format.sample_width_bytes)
        stream.setframerate(self._format.sample_rate)
        self._current = stream
        self._current_frames = 0

    def _close_segment(self) -> None:
        assert self._current is not None
        filename = f"{len(self._segments):06d}.wav"
        self._current.close()
        self._segments.append({"file": filename, "frames": self._current_frames})
        self._current = None
        self._current_frames = 0
        self._write_work_manifest()

    def _consolidate(self) -> None:
        output = self._audio_dir / f"{self._track_name}.wav"
        temporary = output.with_suffix(".wav.tmp")
        with wave.open(str(temporary), "wb") as target:
            target.setnchannels(self._format.channels)
            target.setsampwidth(self._format.sample_width_bytes)
            target.setframerate(self._format.sample_rate)
            for segment in self._segments:
                path = self._work_dir / str(segment["file"])
                with wave.open(str(path), "rb") as source:
                    if (
                        source.getnchannels() != self._format.channels
                        or source.getsampwidth() != self._format.sample_width_bytes
                        or source.getframerate() != self._format.sample_rate
                    ):
                        raise RuntimeError(f"Segment format mismatch: {path}")
                    while frames := source.readframes(65536):
                        target.writeframesraw(frames)
        with temporary.open("rb+") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, output)

    def _remove_validated_work_files(self) -> None:
        shutil.rmtree(self._work_dir)
        work_root = self._work_dir.parent
        with suppress(OSError):
            work_root.rmdir()

    def _write_work_manifest(self) -> None:
        path = self._work_dir / "manifest.json"
        temporary = path.with_suffix(".json.tmp")
        value = {
            "schema_version": 1,
            "track": self._track_name,
            "format": asdict(self._format),
            "frames_written": self._frames_written,
            "segments": self._segments,
        }
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
