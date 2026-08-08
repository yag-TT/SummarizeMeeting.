import wave
from pathlib import Path

import numpy as np

from summarize_meeting.domain.capture import AudioFormat
from summarize_meeting.infrastructure.audio_writer import SegmentedWaveWriter


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
    with wave.open(str(audio_dir / "microphone.wav"), "rb") as stream:
        assert stream.getframerate() == 100
        assert stream.getnchannels() == 2
        assert stream.getnframes() == 250
