from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest

from summarize_meeting.devtools.real_audio_smoke import (
    load_pcm16_wave,
    select_unique_device,
)


@dataclass(frozen=True)
class _Device:
    name: str


def test_select_unique_device_matches_case_insensitively() -> None:
    devices = [_Device("Physical microphone"), _Device("CABLE Output")]

    assert select_unique_device(devices, "cable", label="test") == devices[1]


def test_select_unique_device_rejects_ambiguous_query() -> None:
    devices = [_Device("CABLE Input"), _Device("CABLE Output")]

    with pytest.raises(ValueError, match="1件に絞れる"):
        select_unique_device(devices, "cable", label="test")


def test_load_pcm16_wave_expands_mono_to_stereo(tmp_path: Path) -> None:
    path = tmp_path / "source.wav"
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(np.array([0, 16384, -16384], dtype="<i2").tobytes())

    samples, sample_rate = load_pcm16_wave(path, output_channels=2)

    assert sample_rate == 8000
    assert samples.shape == (3, 2)
    assert np.array_equal(samples[:, 0], samples[:, 1])
    assert samples[1, 0] == pytest.approx(0.5)
