from __future__ import annotations

import json
import wave
from pathlib import Path

import numpy as np
import pytest

from summarize_meeting.processing.audio_enhancement import (
    MODEL_NAME,
    AudioEnhancementService,
    SherpaDpdfNetBackend,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODEL_PATH = PROJECT_ROOT / "models" / "sherpa-onnx" / "speech-enhancement" / MODEL_NAME


@pytest.mark.skipif(not MODEL_PATH.is_file(), reason="audio enhancement model is not installed")
def test_real_model_writes_valid_constant_duration_wave(tmp_path: Path) -> None:
    session = tmp_path / "session"
    audio = session / "audio"
    audio.mkdir(parents=True)
    (session / "analysis").mkdir()
    (audio / "manifest.json").write_text(
        json.dumps({"tracks": {"microphone": {"file": "audio/microphone.wav"}}}),
        encoding="utf-8",
    )
    rng = np.random.default_rng(7)
    timeline = np.arange(24_000, dtype=np.float32) / 48_000
    samples = 0.12 * np.sin(2 * np.pi * 440 * timeline) + 0.02 * rng.standard_normal(
        len(timeline)
    )
    pcm = np.rint(np.clip(samples, -1, 1) * 32767).astype("<i2")
    with wave.open(str(audio / "microphone.wav"), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(48_000)
        stream.writeframes(pcm.tobytes())

    output = AudioEnhancementService(SherpaDpdfNetBackend(MODEL_PATH)).run(session)

    with wave.open(str(output), "rb") as stream:
        assert stream.getframerate() == 48_000
        assert stream.getnchannels() == 1
        assert stream.getsampwidth() == 2
        assert stream.getnframes() == 24_000
    metadata = json.loads(
        (session / "analysis" / "audio_enhancement.json").read_text("utf-8")
    )
    assert metadata["output_quality"]["clipped_samples_percent"] == 0
