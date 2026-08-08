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

    def start(self, target: ScreenTarget) -> None: ...

    def read_latest_frame(self, timeout: float) -> BgrFrame: ...

    def replace_target(self, target: ScreenTarget) -> None: ...

    def stop(self) -> None: ...

    def close(self) -> None: ...
