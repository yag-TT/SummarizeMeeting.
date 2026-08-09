from __future__ import annotations

import sys
from collections.abc import Sequence
from typing import Any

import numpy as np
import sounddevice as sd

from summarize_meeting.capture.audio.base import FloatAudio
from summarize_meeting.domain.capture import AudioDevice, AudioFormat


def _load_soundcard() -> Any:
    # SoundCard connects to PulseAudio while importing on Linux. Keep that
    # connection out of package import so diagnostics and offline workers can
    # run even when the desktop audio server is temporarily unavailable.
    import soundcard

    return soundcard


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
        recorder_options = {
            "samplerate": sample_rate,
            "channels": None,
            "blocksize": max(block_frames * 2, block_frames),
        }
        if sys.platform == "win32":
            recorder_options["exclusive_mode"] = False
        self._context = microphone.recorder(**recorder_options)
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


class SoundDeviceInputStream:
    def __init__(
        self,
        device_name: str,
        *,
        sample_rate: int,
        block_frames: int,
        channels: int,
    ) -> None:
        device_index, max_channels = _find_sounddevice_input(device_name)
        selected_channels = max(1, min(channels, max_channels))
        self._format = AudioFormat(sample_rate, selected_channels)
        stream = sd.InputStream(
            device=device_index,
            samplerate=sample_rate,
            channels=selected_channels,
            dtype="float32",
            blocksize=max(block_frames * 2, block_frames),
            latency="low",
        )
        try:
            stream.start()
        except Exception:
            stream.close()
            raise
        self._stream = stream
        self._closed = False

    @property
    def audio_format(self) -> AudioFormat:
        return self._format

    def read(self, frames: int) -> FloatAudio:
        samples, overflowed = self._stream.read(frames)
        if overflowed:
            raise RuntimeError("sounddevice input overflow")
        value = np.asarray(samples, dtype=np.float32)
        if value.ndim == 1:
            value = value[:, np.newaxis]
        return value

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._stream.stop()
        finally:
            self._stream.close()


class SoundCardAudioBackend:
    def list_input_devices(self) -> Sequence[AudioDevice]:
        sc = _load_soundcard()
        return [
            _to_device(device)
            for device in sc.all_microphones(include_loopback=False)
            if not device.isloopback
        ]

    def list_loopback_devices(self) -> Sequence[AudioDevice]:
        sc = _load_soundcard()
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
        sc = _load_soundcard()
        microphone = sc.get_microphone(device_id, include_loopback=True)
        if microphone is None:
            raise RuntimeError(f"Audio device not found: {device_id}")
        if sys.platform == "win32" and not microphone.isloopback:
            try:
                return SoundDeviceInputStream(
                    str(microphone.name),
                    sample_rate=sample_rate,
                    block_frames=block_frames,
                    channels=int(microphone.channels),
                )
            except Exception as sounddevice_error:
                try:
                    return SoundCardAudioStream(
                        microphone,
                        sample_rate=sample_rate,
                        block_frames=block_frames,
                    )
                except Exception as soundcard_error:
                    raise RuntimeError(
                        "マイクをsounddeviceまたはSoundCardで開始できません: "
                        f"sounddevice={type(sounddevice_error).__name__}: "
                        f"{sounddevice_error}; "
                        f"SoundCard={type(soundcard_error).__name__}: {soundcard_error}"
                    ) from soundcard_error
        try:
            return SoundCardAudioStream(
                microphone,
                sample_rate=sample_rate,
                block_frames=block_frames,
            )
        except Exception as soundcard_error:
            if microphone.isloopback:
                raise
            try:
                return SoundDeviceInputStream(
                    str(microphone.name),
                    sample_rate=sample_rate,
                    block_frames=block_frames,
                    channels=int(microphone.channels),
                )
            except Exception as sounddevice_error:
                raise RuntimeError(
                    "マイクをSoundCardまたはsounddeviceで開始できません: "
                    f"SoundCard={type(soundcard_error).__name__}: {soundcard_error}; "
                    f"sounddevice={type(sounddevice_error).__name__}: {sounddevice_error}"
                ) from sounddevice_error


def _find_sounddevice_input(device_name: str) -> tuple[int, int]:
    host_apis = sd.query_hostapis()
    default_host_index = getattr(sd.default, "hostapi", None)
    host_names = {
        index: value.get("name")
        for index, value in enumerate(host_apis)
        if isinstance(value, dict) and isinstance(value.get("name"), str)
    }
    matches: list[tuple[int, int, int]] = []
    for index, value in enumerate(sd.query_devices()):
        if not isinstance(value, dict):
            continue
        name = value.get("name")
        max_channels = value.get("max_input_channels")
        if (
            isinstance(name, str)
            and name.casefold() == device_name.casefold()
            and isinstance(max_channels, int | float)
            and max_channels > 0
        ):
            host_index = value.get("hostapi")
            host_name = host_names.get(host_index) if isinstance(host_index, int) else None
            priority = _host_api_priority(
                host_name,
                is_default=host_index == default_host_index,
            )
            matches.append((priority, index, int(max_channels)))
    matches.sort()
    if len(matches) != 1:
        best = [match for match in matches if match[0] == matches[0][0]] if matches else []
        if len(best) == 1:
            return best[0][1], best[0][2]
        raise RuntimeError(
            f"sounddevice入力デバイスを1件に特定できません: "
            f"{device_name} (matches={len(matches)})"
        )
    return matches[0][1], matches[0][2]


def _host_api_priority(host_name: str | None, *, is_default: bool) -> int:
    if sys.platform == "win32":
        return {
            "Windows WASAPI": 0,
            "Windows DirectSound": 2,
            "MME": 3,
            "Windows WDM-KS": 4,
        }.get(host_name, 5)
    if host_name == "PulseAudio":
        return 0
    if is_default:
        return 1
    if host_name == "ALSA":
        return 2
    return 3
