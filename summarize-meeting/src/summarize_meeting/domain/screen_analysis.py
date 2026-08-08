from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class OcrLine:
    text: str
    x: float
    y: float
    width: float
    height: float

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ScreenRecognition:
    text: str
    lines: tuple[OcrLine, ...]
    language: str
