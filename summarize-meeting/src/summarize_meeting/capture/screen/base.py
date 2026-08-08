from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

import numpy as np
from numpy.typing import NDArray

from summarize_meeting.domain.capture import ScreenTarget

BgrFrame = NDArray[np.uint8]


class ScreenTargetClosedError(RuntimeError):
    pass


class ScreenTargetPausedError(RuntimeError):
    pass


class ScreenCaptureBackend(Protocol):
    def list_targets(self) -> Sequence[ScreenTarget]: ...

    def capture(self, target: ScreenTarget) -> BgrFrame: ...

    def close(self) -> None: ...
