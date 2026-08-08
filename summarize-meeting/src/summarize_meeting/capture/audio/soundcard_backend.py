from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np
import soundcard as sc

from summarize_meeting.capture.audio.base import FloatAudio
from summarize_meeting.domain.capture import AudioDevice, AudioFormat


def _to_device(microphone: Any) -> AudioDevice:
    return AudioDevice(
        id=str(microphone.id),
        name=str(microphone.name),
        channels=int(microphone.channels),
        is_loopback=bool(microphone.isloopback),
    )


class SoundCardAudioStream:
    def __init__(
        self,
        microphone: Any,
        *,
        sample_rate: int,
        block_frames: int,
    ) -> None:
        self._format = AudioFormat(sample_rate, int(microphone.channels))
        self._context = microphone.recorder(
            samplerate=sample_rate,
            channels=None,
            blocksize=max(block_frames * 2, block_frames),
            exclusive_mode=False,
        )
        self._recorder = self._context.__enter__()
        self._closed = False

    @property
    def audio_format(self) -> AudioFormat:
        return self._format

    def read(self, frames: int) -> FloatAudio:
        samples = np.asarray(self._recorder.record(numframes=frames), dtype=np.float32)
        if samples.ndim == 1:
            samples = samples[:, np.newaxis]
        return samples

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._context.__exit__(None, None, None)


class SoundCardAudioBackend:
    def list_input_devices(self) -> Sequence[AudioDevice]:
        return [
            _to_device(device)
            for device in sc.all_microphones(include_loopback=False)
            if not device.isloopback
        ]

    def list_loopback_devices(self) -> Sequence[AudioDevice]:
        return [
            _to_device(device)
            for device in sc.all_microphones(include_loopback=True)
            if device.isloopback
        ]

    def open_stream(
        self,
        device_id: str,
        *,
        sample_rate: int,
        block_frames: int,
    ) -> SoundCardAudioStream:
        microphone = sc.get_microphone(device_id, include_loopback=True)
        if microphone is None:
            raise RuntimeError(f"Audio device not found: {device_id}")
        return SoundCardAudioStream(
            microphone,
            sample_rate=sample_rate,
            block_frames=block_frames,
        )
