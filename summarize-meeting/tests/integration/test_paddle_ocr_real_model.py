from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter

from summarize_meeting.processing.screen_analysis import (
    PaddleOcrBackend,
    paddle_models_status,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODEL_ROOT = PROJECT_ROOT / "models" / "paddleocr"


@pytest.mark.skipif(
    not all(paddle_models_status(MODEL_ROOT).values()),
    reason="PaddleOCR models are not installed",
)
def test_paddle_ocr_recognizes_generated_mixed_language_image(tmp_path: Path, qapp) -> None:
    image = QImage(1200, 320, QImage.Format.Format_RGB888)
    image.fill(QColor("white"))
    painter = QPainter(image)
    painter.setPen(QColor("black"))
    painter.setFont(QFont("Yu Gothic UI", 48))
    painter.drawText(
        image.rect(),
        Qt.AlignmentFlag.AlignCenter,
        "設計会議 Project Meeting\n期限 8月15日 Deadline 8/15",
    )
    painter.end()
    image_path = tmp_path / "mixed-language.png"
    assert image.save(str(image_path), "PNG")

    result = PaddleOcrBackend(models_directory=MODEL_ROOT).analyze(image_path)

    assert result.lines
    normalized = result.text.casefold().replace(" ", "")
    assert "project" in normalized
    assert "8/15" in normalized
