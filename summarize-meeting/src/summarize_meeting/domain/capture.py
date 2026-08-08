from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class AudioDevice:
    id: str
    name: str
    channels: int
    is_loopback: bool = False


@dataclass(frozen=True, slots=True)
class AudioFormat:
    sample_rate: int
    channels: int
    sample_width_bytes: int = 2


@dataclass(frozen=True, slots=True)
class ScreenTarget:
    id: str
    title: str
    kind: Literal["window", "screen", "portal"] = "window"
