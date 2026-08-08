from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class BackendSpeakerTurn:
    start: float
    end: float
    speaker: int


@dataclass(frozen=True, slots=True)
class SpeakerTurn:
    start: float
    end: float
    audio_start: float
    audio_end: float
    speaker_id: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
