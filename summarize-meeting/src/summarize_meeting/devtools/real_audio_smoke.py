from __future__ import annotations

import argparse
import json
import threading
import time
import wave
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, TypeVar

import numpy as np
import soundcard as sc
from PySide6.QtCore import QCoreApplication

from summarize_meeting.application.recording_controller import RecordingController
from summarize_meeting.application.transcription_controller import TranscriptionController
from summarize_meeting.infrastructure.paths import PortableAppPaths


class NamedDevice(Protocol):
    name: str


Device = TypeVar("Device", bound=NamedDevice)


def select_unique_device(
    devices: Sequence[Device],
    query: str,
    *,
    label: str,
) -> Device:
    normalized = query.casefold().strip()
    matches = [device for device in devices if normalized in str(device.name).casefold()]
    if len(matches) != 1:
        names = ", ".join(str(device.name) for device in devices) or "なし"
        raise ValueError(
            f"{label}は1件に絞れる名前を指定してください: query={query!r}, matches={len(matches)}, "
            f"available={names}"
        )
    return matches[0]


def load_pcm16_wave(path: Path, *, output_channels: int) -> tuple[np.ndarray, int]:
    if output_channels <= 0:
        raise ValueError("output_channels must be positive")
    with wave.open(str(path), "rb") as stream:
        if stream.getsampwidth() != 2:
            raise ValueError("テストWAVはPCM16である必要があります")
        channels = stream.getnchannels()
        sample_rate = stream.getframerate()
        frames = stream.readframes(stream.getnframes())
    samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    samples = samples.reshape(-1, channels)
    if channels == output_channels:
        return samples, sample_rate
    if channels == 1:
        return np.repeat(samples, output_channels, axis=1), sample_rate
    if output_channels == 1:
        return samples.mean(axis=1, keepdims=True), sample_rate
    raise ValueError(f"WAVの{channels}chを再生デバイスの{output_channels}chへ変換できません")


def run_smoke(
    *,
    source_wave: Path,
    microphone_query: str,
    loopback_query: str,
    speaker_query: str,
    title: str,
    transcribe: bool,
) -> dict[str, object]:
    app = QCoreApplication.instance() or QCoreApplication([])
    paths = PortableAppPaths.discover()
    paths.ensure_writable()
    recording = RecordingController(paths)
    microphone = select_unique_device(
        recording.list_input_devices(), microphone_query, label="マイク"
    )
    loopback = select_unique_device(
        recording.list_loopback_devices(), loopback_query, label="PC音声"
    )
    speaker = select_unique_device(sc.all_speakers(), speaker_query, label="再生デバイス")
    samples, sample_rate = load_pcm16_wave(source_wave, output_channels=int(speaker.channels))

    started = threading.Event()
    finished = threading.Event()
    recording_errors: list[str] = []
    recording.session_started.connect(lambda _path: started.set())
    recording.session_finished.connect(lambda _path: finished.set())
    recording.session_start_failed.connect(
        lambda _path, message: (recording_errors.append(message), started.set())
    )
    recording.fatal_error.connect(recording_errors.append)
    session = recording.start_session(
        title=title,
        microphone=microphone,
        system_audio=loopback,
        screen_target=None,
    )
    if not _wait(started, app, timeout=15):
        recording.stop_for_shutdown(timeout_seconds=10)
        raise TimeoutError("録音開始を15秒以内に確認できませんでした")
    if recording_errors:
        raise RuntimeError(recording_errors[-1])

    time.sleep(0.5)
    with speaker.player(
        samplerate=sample_rate,
        channels=int(speaker.channels),
        blocksize=2048,
    ) as player:
        player.play(samples)
    time.sleep(0.5)
    recording.stop_session()
    if not _wait(finished, app, timeout=60):
        recording.stop_for_shutdown(timeout_seconds=10)
        raise TimeoutError("録音確定を60秒以内に確認できませんでした")
    if recording_errors:
        raise RuntimeError(recording_errors[-1])

    result: dict[str, object] = {
        "session": str(session),
        "microphone": microphone.name,
        "loopback": loopback.name,
        "speaker": str(speaker.name),
        "transcribed": False,
    }
    if not transcribe:
        return result

    transcription = TranscriptionController(paths)
    transcription_done = threading.Event()
    transcription_errors: list[str] = []
    transcript_paths: list[str] = []
    transcription.job_finished.connect(
        lambda _session, output: (transcript_paths.append(output), transcription_done.set())
    )
    transcription.job_failed.connect(
        lambda _session, message: (transcription_errors.append(message), transcription_done.set())
    )
    transcription.job_canceled.connect(lambda _session: transcription_done.set())
    transcription.start(session)
    if not _wait(transcription_done, app, timeout=300):
        transcription.cancel()
        raise TimeoutError("文字起こしを300秒以内に確認できませんでした")
    if transcription_errors:
        raise RuntimeError(transcription_errors[-1])
    if not transcript_paths:
        raise RuntimeError("文字起こしが完了しませんでした")
    payload = json.loads((session / "analysis" / "transcription.json").read_text("utf-8"))
    result.update(
        {
            "transcribed": True,
            "transcript": transcript_paths[-1],
            "segment_count": len(payload.get("segments", [])),
            "runtime_devices": [track.get("runtime_device") for track in payload["tracks"]],
        }
    )
    return result


def _wait(event: threading.Event, app: QCoreApplication, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if event.wait(timeout=0.05):
            app.processEvents()
            return True
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Record a test WAV through real Windows audio devices and optionally transcribe it"
        )
    )
    parser.add_argument("--source-wave", required=True, type=Path)
    parser.add_argument("--microphone", required=True, help="Unique part of the microphone name")
    parser.add_argument("--loopback", required=True, help="Unique part of the loopback name")
    parser.add_argument("--speaker", required=True, help="Unique part of the speaker name")
    parser.add_argument("--title", default="Phase2 real audio smoke")
    parser.add_argument("--skip-transcription", action="store_true")
    args = parser.parse_args(argv)
    result = run_smoke(
        source_wave=args.source_wave.resolve(),
        microphone_query=args.microphone,
        loopback_query=args.loopback,
        speaker_query=args.speaker,
        title=args.title,
        transcribe=not args.skip_transcription,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
