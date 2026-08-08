from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

import numpy as np
from numpy.typing import NDArray

from summarize_meeting.domain.capture import AudioDevice, AudioFormat

FloatAudio = NDArray[np.float32]


class AudioStream(Protocol):
    @property
    def audio_format(self) -> AudioFormat: ...

    def read(self, frames: int) -> FloatAudio: ...

    def close(self) -> None: ...


class AudioBackend(Protocol):
    def list_input_devices(self) -> Sequence[AudioDevice]: ...

    def list_loopback_devices(self) -> Sequence[AudioDevice]: ...

    def open_stream(
        self,
        device_id: str,
        *,
        sample_rate: int,
        block_frames: int,
    ) -> AudioStream: ...
