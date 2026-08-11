import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from summarize_meeting.infrastructure import screenshot_store as store_module
from summarize_meeting.infrastructure.screenshot_store import (
    ScreenshotSaveError,
    ScreenshotStore,
)


def test_screenshot_store_verifies_png_and_appends_metadata(tmp_path: Path) -> None:
    store = ScreenshotStore(tmp_path)
    frame = np.full((10, 12, 3), (10, 20, 30), dtype=np.uint8)

    filename = store.save(
        frame,
        timestamp_ms=123,
        reason="initial",
        metrics={"changed_ratio": 1.0, "mean_abs_diff": 255.0},
    )

    assert filename == "000001.png"
    assert store.count == 1
    decoded = cv2.imread(str(tmp_path / filename), cv2.IMREAD_COLOR)
    assert decoded is not None
    assert decoded.shape == frame.shape
    event = json.loads((tmp_path / "events.jsonl").read_text(encoding="utf-8"))
    assert event["sequence"] == 1
    assert event["timestamp_ms"] == 123
    assert event["width"] == 12
    assert event["height"] == 10
    assert not (tmp_path / "000001.png.tmp").exists()


def test_screenshot_store_keeps_temp_when_decode_verification_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = ScreenshotStore(tmp_path)
    frame = np.zeros((10, 12, 3), dtype=np.uint8)
    monkeypatch.setattr(store_module.cv2, "imdecode", lambda *_args, **_kwargs: None)

    with pytest.raises(ScreenshotSaveError, match="verification"):
        store.save(
            frame,
            timestamp_ms=0,
            reason="initial",
            metrics={"changed_ratio": 1.0, "mean_abs_diff": 255.0},
        )

    assert store.count == 0
    assert not (tmp_path / "000001.png").exists()
    assert (tmp_path / "000001.png.tmp").exists()


def test_screenshot_store_rolls_back_png_when_event_publish_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = ScreenshotStore(tmp_path)
    frame = np.zeros((10, 12, 3), dtype=np.uint8)

    def fail_events(*_args, **_kwargs) -> None:
        raise OSError("event storage unavailable")

    monkeypatch.setattr(store_module, "write_bytes_atomic", fail_events)

    with pytest.raises(ScreenshotSaveError, match="event storage unavailable"):
        store.save(
            frame,
            timestamp_ms=0,
            reason="initial",
            metrics={"changed_ratio": 1.0, "mean_abs_diff": 255.0},
        )

    assert store.count == 0
    assert not (tmp_path / "000001.png").exists()
    assert not (tmp_path / "events.jsonl").exists()
