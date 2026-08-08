from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class OcrLine:
    text: str
    x: float
    y: float
    width: float
    height: float
    confidence: float = 1.0

    def to_dict(self) -> dict[str, object]:
        # Keep analysis/screens.json schema version 1 unchanged.
        return {
            "text": self.text,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
        }


@dataclass(frozen=True, slots=True)
class ScreenRecognition:
    text: str
    lines: tuple[OcrLine, ...]
    language: str
