from __future__ import annotations

import json
import wave
from pathlib import Path

import pytest

from summarize_meeting.processing.transcription import (
    BackendSegment,
    BackendTranscription,
    TranscriptionError,
    TranscriptionService,
)


class _Backend:
    def __init__(self) -> None:
        self.paths: list[Path] = []

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str,
        progress_callback=None,
    ) -> BackendTranscription:
        self.paths.append(audio_path)
        if progress_callback is not None:
            progress_callback(0.5)
        if audio_path.name == "microphone.wav":
            segments = (BackendSegment(0.5, 1.25, "確認します", -0.1, 0.01),)
        else:
            segments = (BackendSegment(0.1, 0.8, "お願いします", -0.2, 0.02),)
        return BackendTranscription(segments, language, 0.99, 2.0)


def _write_wave(path: Path) -> None:
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16_000)
        stream.writeframes(b"\0\0" * 160)


def _session(tmp_path: Path) -> Path:
    session = tmp_path / "meeting"
    audio = session / "audio"
    audio.mkdir(parents=True)
    (session / "analysis").mkdir()
    (session / "output").mkdir()
    _write_wave(audio / "microphone.wav")
    _write_wave(audio / "system.wav")
    (audio / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "tracks": {
                    "microphone": {
                        "file": "microphone.wav",
                        "estimated_start_offset_ms": 100,
                    },
                    "system_audio": {
                        "file": "system.wav",
                        "estimated_start_offset_ms": 700,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    return session


def test_transcription_merges_tracks_on_session_timeline(tmp_path: Path) -> None:
    session = _session(tmp_path)
    backend = _Backend()
    progress: list[int] = []
    service = TranscriptionService(backend, model_name="test-model")

    output = service.run(
        session,
        language="ja",
        progress_callback=lambda percent, _message: progress.append(percent),
    )

    assert output == session / "output" / "transcript.md"
    value = json.loads((session / "analysis" / "transcription.json").read_text("utf-8"))
    assert value["status"] == "SUCCEEDED"
    assert value["model"] == "test-model"
    assert [(item["source"], item["start"]) for item in value["segments"]] == [
        ("microphone", 0.6),
        ("system", 0.8),
    ]
    assert [item["start_offset_ms"] for item in value["tracks"]] == [100, 700]
    markdown = output.read_text(encoding="utf-8")
    assert "## 00:00:00.600" in markdown
    assert "**自分**" in markdown
    assert "**PC音声**" in markdown
    assert progress[-1] == 100


def test_transcription_rejects_manifest_path_traversal(tmp_path: Path) -> None:
    session = _session(tmp_path)
    manifest_path = session / "audio" / "manifest.json"
    value = json.loads(manifest_path.read_text("utf-8"))
    value["tracks"]["microphone"]["file"] = "../outside.wav"
    manifest_path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(TranscriptionError, match="音声ファイル名が不正"):
        TranscriptionService(_Backend(), model_name="test-model").run(session)


def test_transcription_requires_at_least_one_supported_track(tmp_path: Path) -> None:
    session = _session(tmp_path)
    (session / "audio" / "manifest.json").write_text(
        json.dumps({"schema_version": 1, "tracks": {}}),
        encoding="utf-8",
    )

    with pytest.raises(TranscriptionError, match="文字起こし可能な音声"):
        TranscriptionService(_Backend(), model_name="test-model").run(session)
