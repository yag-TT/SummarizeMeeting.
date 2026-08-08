from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from summarize_meeting.domain.screen_analysis import OcrLine, ScreenRecognition
from summarize_meeting.processing.screen_analysis import (
    ScreenAnalysisError,
    ScreenAnalysisService,
    derive_screen_understanding,
)


class _FakeBackend:
    runtime_name = "fake-ocr"
    language = "ja"

    def __init__(self, results: dict[str, ScreenRecognition | Exception]) -> None:
        self._results = results
        self.calls: list[str] = []

    def analyze(self, image_path: Path) -> ScreenRecognition:
        self.calls.append(image_path.name)
        value = self._results[image_path.name]
        if isinstance(value, Exception):
            raise value
        return value


def _recognition(*lines: str) -> ScreenRecognition:
    return ScreenRecognition(
        text="\n".join(lines),
        lines=tuple(
            OcrLine(text=text, x=10, y=index * 20, width=200, height=18)
            for index, text in enumerate(lines)
        ),
        language="ja",
    )


def _session(tmp_path: Path, events: list[dict[str, object]]) -> Path:
    session = tmp_path / "session"
    screenshots = session / "screenshots"
    screenshots.mkdir(parents=True)
    (session / "session.json").write_text('{"status":"RECORDED"}', encoding="utf-8")
    (screenshots / "events.jsonl").write_text(
        "\n".join(json.dumps(value, ensure_ascii=False) for value in events) + "\n",
        encoding="utf-8",
    )
    for event in events:
        filename = event.get("file")
        if isinstance(filename, str) and Path(filename).parts == (Path(filename).name,):
            image = np.full((80, 120, 3), 255, dtype=np.uint8)
            success, encoded = cv2.imencode(".png", image)
            assert success
            (screenshots / filename).write_bytes(encoded.tobytes())
    return session


def _event(sequence: int, timestamp_ms: int, file: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "sequence": sequence,
        "timestamp_ms": timestamp_ms,
        "file": file,
        "width": 120,
        "height": 80,
        "reason": "changed",
        "metrics": {"changed_ratio": 0.4},
    }


def test_service_generates_timestamped_screen_analysis(tmp_path: Path) -> None:
    session = _session(
        tmp_path,
        [_event(2, 2500, "000002.png"), _event(1, 1000, "000001.png")],
    )
    backend = _FakeBackend(
        {
            "000001.png": _recognition("Google Chrome", "API仕様変更", "期限 8月15日"),
            "000002.png": _recognition("Microsoft Teams", "設計会議", "担当 田中"),
        }
    )
    progress: list[int] = []

    output = ScreenAnalysisService(backend).run(
        session,
        progress_callback=lambda percent, _message: progress.append(percent),
    )

    value = json.loads(output.read_text(encoding="utf-8"))
    assert value["status"] == "SUCCEEDED"
    assert value["runtime"] == "fake-ocr"
    assert value["statistics"] == {"total": 2, "succeeded": 2, "failed": 0}
    assert [screen["sequence"] for screen in value["screens"]] == [1, 2]
    assert value["screens"][0]["timestamp"] == 1.0
    assert value["screens"][0]["image"] == "screenshots/000001.png"
    assert value["screens"][0]["type"] == "browser"
    assert value["screens"][0]["important"] == ["API仕様変更", "期限 8月15日"]
    assert progress[0] == 0
    assert progress[-1] == 100


def test_service_keeps_success_when_one_image_fails(tmp_path: Path) -> None:
    session = _session(
        tmp_path,
        [_event(1, 1000, "000001.png"), _event(2, 2000, "000002.png")],
    )
    (session / "screenshots" / "000002.png").unlink()
    backend = _FakeBackend({"000001.png": _recognition("画面"), "000002.png": _recognition()})

    output = ScreenAnalysisService(backend).run(session)

    value = json.loads(output.read_text(encoding="utf-8"))
    assert value["statistics"] == {"total": 2, "succeeded": 1, "failed": 1}
    assert value["screens"][1]["status"] == "FAILED"
    assert "スクリーンショットがありません" in value["screens"][1]["error_message"]


def test_all_failures_preserve_previous_result(tmp_path: Path) -> None:
    session = _session(tmp_path, [_event(1, 1000, "000001.png")])
    analysis = session / "analysis"
    analysis.mkdir()
    previous = analysis / "screens.json"
    previous.write_text('{"status":"SUCCEEDED","old":true}', encoding="utf-8")
    backend = _FakeBackend({"000001.png": RuntimeError("ocr failed")})

    with pytest.raises(ScreenAnalysisError, match="すべて"):
        ScreenAnalysisService(backend).run(session)

    assert json.loads(previous.read_text(encoding="utf-8"))["old"] is True


def test_service_rejects_path_traversal(tmp_path: Path) -> None:
    session = _session(tmp_path, [_event(1, 1000, "../outside.png")])
    backend = _FakeBackend({})

    with pytest.raises(ScreenAnalysisError, match="fileが不正"):
        ScreenAnalysisService(backend).run(session)


def test_understanding_does_not_invent_important_items() -> None:
    value = derive_screen_understanding(
        _recognition("PowerPoint", "開発計画", "来週までにテスト", "単なる説明")
    )

    assert value["type"] == "presentation"
    assert value["title"] == "PowerPoint"
    assert value["important"] == ["来週までにテスト"]
