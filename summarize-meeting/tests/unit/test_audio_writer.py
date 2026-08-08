import wave
from pathlib import Path

import numpy as np
import pytest

from summarize_meeting.domain.capture import AudioFormat
from summarize_meeting.infrastructure.audio_writer import (
    SegmentedWaveWriter,
    WaveValidationError,
    validate_wave_file,
)


def test_segmented_writer_consolidates_segments(tmp_path: Path) -> None:
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    writer = SegmentedWaveWriter(
        audio_dir,
        "microphone",
        AudioFormat(sample_rate=100, channels=2),
        segment_seconds=1,
    )
    samples = np.linspace(-1.0, 1.0, 250, dtype=np.float32)
    writer.write(np.column_stack((samples, -samples)))

    stats = writer.close()

    assert stats.frames_written == 250
    assert stats.segments == 3
    assert stats.audio_duration_ms == 2_500.0
    assert stats.validated
    assert stats.work_files_removed
    assert stats.work_cleanup_error is None
    assert not (audio_dir / ".work" / "microphone").exists()
    with wave.open(str(audio_dir / "microphone.wav"), "rb") as stream:
        assert stream.getframerate() == 100
        assert stream.getnchannels() == 2
        assert stream.getnframes() == 250


def test_validate_wave_file_rejects_format_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "wrong-format.wav"
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(200)
        stream.writeframes(b"\0\0" * 20)

    with pytest.raises(WaveValidationError, match="format mismatch"):
        validate_wave_file(
            path,
            expected_format=AudioFormat(sample_rate=100, channels=1),
            expected_frames=20,
        )


def test_validate_wave_file_rejects_declared_frame_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "wrong-frames.wav"
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(100)
        stream.writeframes(b"\0\0" * 19)

    with pytest.raises(WaveValidationError, match="frame count mismatch"):
        validate_wave_file(
            path,
            expected_format=AudioFormat(sample_rate=100, channels=1),
            expected_frames=20,
        )


def test_validate_wave_file_rejects_truncated_pcm_data(tmp_path: Path) -> None:
    path = tmp_path / "truncated.wav"
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(100)
        stream.writeframes(b"\0\0" * 20)
    path.write_bytes(path.read_bytes()[:-2])

    with pytest.raises(WaveValidationError, match="readable frame count mismatch"):
        validate_wave_file(
            path,
            expected_format=AudioFormat(sample_rate=100, channels=1),
            expected_frames=20,
        )


class _CorruptingSegmentedWaveWriter(SegmentedWaveWriter):
    def _consolidate(self) -> None:
        super()._consolidate()
        output = self._audio_dir / f"{self._track_name}.wav"  # noqa: SLF001
        output.write_bytes(b"not-a-wave")


class _CleanupFailingSegmentedWaveWriter(SegmentedWaveWriter):
    def _remove_validated_work_files(self) -> None:
        raise OSError("work directory is busy")


def test_segmented_writer_keeps_work_files_when_final_validation_fails(
    tmp_path: Path,
) -> None:
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    writer = _CorruptingSegmentedWaveWriter(
        audio_dir,
        "microphone",
        AudioFormat(sample_rate=100, channels=1),
        segment_seconds=1,
    )
    writer.write(np.full((20, 1), 0.1, dtype=np.float32))

    with pytest.raises(WaveValidationError, match="cannot be opened"):
        writer.close()

    assert (audio_dir / ".work" / "microphone" / "000000.wav").is_file()
    assert (audio_dir / ".work" / "microphone" / "manifest.json").is_file()


def test_segmented_writer_reports_cleanup_failure_after_successful_validation(
    tmp_path: Path,
) -> None:
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    writer = _CleanupFailingSegmentedWaveWriter(
        audio_dir,
        "system",
        AudioFormat(sample_rate=100, channels=1),
    )
    writer.write(np.full((20, 1), 0.1, dtype=np.float32))

    stats = writer.close()

    assert stats.validated
    assert not stats.work_files_removed
    assert stats.work_cleanup_error == "work directory is busy"
    assert (audio_dir / "system.wav").is_file()
    assert (audio_dir / ".work" / "system" / "000000.wav").is_file()
