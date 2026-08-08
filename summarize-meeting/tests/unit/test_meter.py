import numpy as np
import pytest

from summarize_meeting.capture.audio.meter import normalized_rms


def test_meter_maps_silence_to_zero() -> None:
    assert normalized_rms(np.zeros(100, dtype=np.float32)) == 0.0


def test_meter_maps_full_scale_to_one() -> None:
    assert normalized_rms(np.ones(100, dtype=np.float32)) == pytest.approx(1.0)


def test_meter_handles_nan_without_propagating() -> None:
    assert normalized_rms(np.array([np.nan, 0.0], dtype=np.float32)) == 0.0
