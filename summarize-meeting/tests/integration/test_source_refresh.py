from __future__ import annotations

import threading
import time
from pathlib import Path

import numpy as np
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from summarize_meeting.application.recording_controller import (
    CaptureSourcesSnapshot,
    RecordingController,
)
from summarize_meeting.domain.capture import AudioDevice, ScreenTarget
from summarize_meeting.infrastructure.paths import PortableAppPaths


class _BlockingAudioBackend:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def list_input_devices(self):
        self.started.set()
        self.release.wait(2.0)
        return [AudioDevice("mic", "Conference mic", 1)]

    def list_loopback_devices(self):
        return [AudioDevice("speaker", "Speakers", 2, is_loopback=True)]


class _ScreenBackend:
    def list_targets(self):
        return [ScreenTarget("screen", "Planning deck")]


class _PreviewScreenBackend(_ScreenBackend):
    def __init__(self) -> None:
        self.started: list[ScreenTarget] = []
        self.stops = 0

    def start(self, target: ScreenTarget) -> None:
        self.started.append(target)

    def read_latest_frame(self, timeout: float):
        assert timeout == 120.0
        return np.full((4, 6, 3), 42, dtype=np.uint8)

    def stop(self) -> None:
        self.stops += 1


class _PartiallyFailingAudioBackend:
    def list_input_devices(self):
        raise RuntimeError("microphone service unavailable")

    def list_loopback_devices(self):
        return [AudioDevice("speaker", "Speakers", 2, is_loopback=True)]


class _PreviewAudioStream:
    def __init__(self) -> None:
        self.closed = False

    def read(self, frames: int):
        time.sleep(0.005)
        return np.full((frames, 1), 0.1, dtype=np.float32)

    def close(self) -> None:
        self.closed = True


class _PreviewAudioBackend:
    def __init__(self) -> None:
        self.opened: list[str] = []
        self.streams: list[_PreviewAudioStream] = []

    def open_stream(self, device_id: str, *, sample_rate: int, block_frames: int):
        assert sample_rate == 48_000
        assert block_frames == 4_800
        self.opened.append(device_id)
        stream = _PreviewAudioStream()
        self.streams.append(stream)
        return stream


def _controller(tmp_path: Path) -> RecordingController:
    paths = PortableAppPaths(tmp_path)
    paths.ensure_writable()
    return RecordingController(paths)


def _wait_for(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.01)
    QCoreApplication.processEvents()
    assert predicate()


def test_source_refresh_returns_without_blocking_ui_thread(
    tmp_path: Path,
    qapp: QApplication,
) -> None:
    controller = _controller(tmp_path)
    backend = _BlockingAudioBackend()
    controller._audio_backend = backend  # type: ignore[assignment]  # noqa: SLF001
    controller._screen_backend = _ScreenBackend()  # type: ignore[assignment]  # noqa: SLF001
    results: list[tuple[int, CaptureSourcesSnapshot]] = []
    controller.sources_refreshed.connect(
        lambda request_id, snapshot: results.append((request_id, snapshot))
    )

    started_at = time.monotonic()
    controller.refresh_sources_async(42)
    elapsed = time.monotonic() - started_at

    assert elapsed < 0.2
    assert backend.started.wait(1.0)
    assert results == []

    backend.release.set()
    _wait_for(lambda: bool(results))

    request_id, snapshot = results[0]
    assert request_id == 42
    assert [device.name for device in snapshot.microphones] == ["Conference mic"]
    assert [device.name for device in snapshot.system_audio] == ["Speakers"]
    assert [target.title for target in snapshot.screens] == ["Planning deck"]
    assert snapshot.errors == ()


def test_source_refresh_returns_partial_results_when_one_enumeration_fails(
    tmp_path: Path,
    qapp: QApplication,
) -> None:
    controller = _controller(tmp_path)
    controller._audio_backend = _PartiallyFailingAudioBackend()  # type: ignore[assignment]  # noqa: SLF001
    controller._screen_backend = _ScreenBackend()  # type: ignore[assignment]  # noqa: SLF001
    results: list[CaptureSourcesSnapshot] = []
    controller.sources_refreshed.connect(lambda _request_id, snapshot: results.append(snapshot))

    controller.refresh_sources_async(7)
    _wait_for(lambda: bool(results))

    snapshot = results[0]
    assert snapshot.microphones == ()
    assert [device.name for device in snapshot.system_audio] == ["Speakers"]
    assert [target.title for target in snapshot.screens] == ["Planning deck"]
    assert len(snapshot.errors) == 1
    assert "マイク一覧" in snapshot.errors[0]


def test_screen_preview_captures_one_frame_without_blocking_ui_thread(
    tmp_path: Path,
    qapp: QApplication,
) -> None:
    controller = _controller(tmp_path)
    backend = _PreviewScreenBackend()
    controller._screen_backend = backend  # type: ignore[assignment]  # noqa: SLF001
    results: list[tuple[int, np.ndarray]] = []
    controller.screen_preview_ready.connect(
        lambda request_id, frame: results.append((request_id, frame))
    )
    target = ScreenTarget("screen", "Planning deck")

    started_at = time.monotonic()
    controller.preview_screen_target_async(11, target)
    elapsed = time.monotonic() - started_at

    assert elapsed < 0.2
    _wait_for(lambda: bool(results))
    request_id, frame = results[0]
    assert request_id == 11
    assert frame.shape == (4, 6, 3)
    assert backend.started == [target]
    assert backend.stops == 1
    assert not controller.is_screen_previewing


def test_audio_preview_monitors_both_sources_until_cancelled(
    tmp_path: Path,
    qapp: QApplication,
) -> None:
    controller = _controller(tmp_path)
    backend = _PreviewAudioBackend()
    controller._audio_backend = backend  # type: ignore[assignment]  # noqa: SLF001
    meters: list[tuple[str, float]] = []
    results: list[tuple[int, tuple[str, ...]]] = []
    controller.meter_changed.connect(
        lambda component, level: meters.append((component, level))
    )
    controller.audio_preview_finished.connect(
        lambda request_id, errors: results.append((request_id, errors))
    )

    controller.preview_audio_sources_async(
        12,
        AudioDevice("mic", "Conference mic", 1),
        AudioDevice("speaker", "Speakers", 2, is_loopback=True),
    )

    _wait_for(lambda: {component for component, level in meters if level > 0} == {
        "microphone",
        "system_audio",
    })
    assert controller.is_audio_previewing
    assert sorted(backend.opened) == ["mic", "speaker"]

    controller.cancel_audio_preview()
    _wait_for(lambda: bool(results))

    assert results == [(12, ())]
    assert all(stream.closed for stream in backend.streams)
    assert not controller.is_audio_previewing
