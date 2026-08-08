from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    start: float
    end: float
    source: str
    text: str
    avg_logprob: float | None = None
    no_speech_prob: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


@dataclass(frozen=True, slots=True)
class TranscribedTrack:
    source: str
    file: str
    start_offset_ms: int
    detected_language: str
    language_probability: float
    duration_seconds: float
    segment_count: int
    runtime_device: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
