from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from summarize_meeting.capture.audio import soundcard_backend as backend_module
from summarize_meeting.capture.audio.soundcard_backend import (
    SoundCardAudioBackend,
    SoundCardAudioStream,
    _find_sounddevice_input,
)


@dataclass
class _Microphone:
    name: str = "マイク (Brio 100)"
    channels: int = 1
    isloopback: bool = False

    def recorder(self, **_kwargs):
        raise AssertionError("unsupported mix format")


class _RecorderContext:
    def __enter__(self):
        return object()

    def __exit__(self, *_args) -> None:
        pass


@pytest.mark.parametrize(
    ("platform", "expects_exclusive_mode"),
    [("linux", False), ("win32", True)],
)
def test_soundcard_stream_only_passes_exclusive_mode_on_windows(
    monkeypatch,
    platform: str,
    expects_exclusive_mode: bool,
) -> None:
    options: dict[str, object] = {}

    class Microphone:
        channels = 1

        def recorder(self, **kwargs):
            options.update(kwargs)
            return _RecorderContext()

    monkeypatch.setattr(backend_module.sys, "platform", platform)

    stream = SoundCardAudioStream(
        Microphone(),
        sample_rate=48_000,
        block_frames=4_800,
    )
    stream.close()

    assert ("exclusive_mode" in options) is expects_exclusive_mode


def test_non_windows_physical_microphone_falls_back_to_sounddevice(monkeypatch) -> None:
    microphone = _Microphone()
    expected = object()
    received: list[tuple[str, int, int, int]] = []
    soundcard = SimpleNamespace(get_microphone=lambda *_args, **_kwargs: microphone)
    monkeypatch.setattr(backend_module, "_load_soundcard", lambda: soundcard)
    monkeypatch.setattr(backend_module.sys, "platform", "linux")

    def create_stream(name, *, sample_rate, block_frames, channels):
        received.append((name, sample_rate, block_frames, channels))
        return expected

    monkeypatch.setattr(backend_module, "SoundDeviceInputStream", create_stream)

    result = SoundCardAudioBackend().open_stream(
        "device-id",
        sample_rate=48_000,
        block_frames=4_800,
    )

    assert result is expected
    assert received == [("マイク (Brio 100)", 48_000, 4_800, 1)]


def test_windows_physical_microphone_prefers_sounddevice(monkeypatch) -> None:
    microphone = _Microphone()
    expected = object()
    soundcard = SimpleNamespace(get_microphone=lambda *_args, **_kwargs: microphone)
    monkeypatch.setattr(backend_module, "_load_soundcard", lambda: soundcard)
    monkeypatch.setattr(backend_module.sys, "platform", "win32")
    monkeypatch.setattr(
        backend_module,
        "SoundDeviceInputStream",
        lambda *_args, **_kwargs: expected,
    )

    result = SoundCardAudioBackend().open_stream(
        "device-id",
        sample_rate=48_000,
        block_frames=4_800,
    )

    assert result is expected


def test_windows_physical_microphone_falls_back_to_soundcard(monkeypatch) -> None:
    microphone = _Microphone()
    expected = object()
    soundcard = SimpleNamespace(get_microphone=lambda *_args, **_kwargs: microphone)
    monkeypatch.setattr(backend_module, "_load_soundcard", lambda: soundcard)
    monkeypatch.setattr(backend_module.sys, "platform", "win32")

    def fail_sounddevice(*_args, **_kwargs):
        raise RuntimeError("WASAPI unavailable")

    monkeypatch.setattr(backend_module, "SoundDeviceInputStream", fail_sounddevice)
    monkeypatch.setattr(
        backend_module,
        "SoundCardAudioStream",
        lambda *_args, **_kwargs: expected,
    )

    result = SoundCardAudioBackend().open_stream(
        "device-id",
        sample_rate=48_000,
        block_frames=4_800,
    )

    assert result is expected


def test_loopback_does_not_use_input_only_fallback(monkeypatch) -> None:
    microphone = _Microphone(isloopback=True)
    soundcard = SimpleNamespace(get_microphone=lambda *_args, **_kwargs: microphone)
    monkeypatch.setattr(backend_module, "_load_soundcard", lambda: soundcard)

    with pytest.raises(AssertionError, match="unsupported mix format"):
        SoundCardAudioBackend().open_stream(
            "device-id",
            sample_rate=48_000,
            block_frames=4_800,
        )


def test_find_sounddevice_input_prefers_default_host_api(monkeypatch) -> None:
    monkeypatch.setattr(backend_module.sd, "default", SimpleNamespace(hostapi=1))
    monkeypatch.setattr(
        backend_module.sd,
        "query_hostapis",
        lambda: [{"name": "MME"}, {"name": "Windows WASAPI"}],
    )
    monkeypatch.setattr(
        backend_module.sd,
        "query_devices",
        lambda: [
            {"name": "マイク (Brio 100)", "hostapi": 0, "max_input_channels": 1},
            {"name": "マイク (Brio 100)", "hostapi": 1, "max_input_channels": 2},
        ],
    )

    assert _find_sounddevice_input("マイク (Brio 100)") == (1, 2)


def test_find_sounddevice_input_prefers_wasapi_over_default_mme(monkeypatch) -> None:
    monkeypatch.setattr(backend_module.sys, "platform", "win32")
    monkeypatch.setattr(backend_module.sd, "default", SimpleNamespace(hostapi=0))
    monkeypatch.setattr(
        backend_module.sd,
        "query_hostapis",
        lambda: [{"name": "MME"}, {"name": "Windows WASAPI"}],
    )
    monkeypatch.setattr(
        backend_module.sd,
        "query_devices",
        lambda: [
            {"name": "マイク (Brio 100)", "hostapi": 0, "max_input_channels": 1},
            {"name": "マイク (Brio 100)", "hostapi": 1, "max_input_channels": 1},
        ],
    )

    assert _find_sounddevice_input("マイク (Brio 100)") == (1, 1)


def test_find_sounddevice_input_rejects_ambiguous_name(monkeypatch) -> None:
    monkeypatch.setattr(backend_module.sd, "default", SimpleNamespace(hostapi=None))
    monkeypatch.setattr(
        backend_module.sd,
        "query_hostapis",
        lambda: [{"name": "ALSA"}],
    )
    monkeypatch.setattr(
        backend_module.sd,
        "query_devices",
        lambda: [
            {"name": "USB Mic", "hostapi": 0, "max_input_channels": 1},
            {"name": "USB Mic", "hostapi": 0, "max_input_channels": 2},
        ],
    )

    with pytest.raises(RuntimeError, match="1件に特定できません"):
        _find_sounddevice_input("USB Mic")
