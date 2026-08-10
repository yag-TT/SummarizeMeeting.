from __future__ import annotations

import json
import wave
from pathlib import Path

import pytest

from summarize_meeting.processing.transcription import (
    BackendSegment,
    BackendTranscription,
    FasterWhisperBackend,
    TranscriptionError,
    TranscriptionService,
    _is_cuda_runtime_error,
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
    _write_wave(audio / "system_audio.wav")
    (session / "session.json").write_text(
        json.dumps({"schema_version": 2, "status": "RECORDED"}),
        encoding="utf-8",
    )
    (audio / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "tracks": {
                    "microphone": {
                        "file": "microphone.wav",
                        "estimated_start_offset_ms": 100,
                    },
                    "system_audio": {
                        "file": "system_audio.wav",
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
        ("system_audio", 0.8),
    ]
    assert [item["start_offset_ms"] for item in value["tracks"]] == [100, 700]
    assert [item["runtime_device"] for item in value["tracks"]] == ["unknown", "unknown"]
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


def test_transcription_rejects_legacy_session_relative_audio_path(tmp_path: Path) -> None:
    session = _session(tmp_path)
    manifest_path = session / "audio" / "manifest.json"
    value = json.loads(manifest_path.read_text("utf-8"))
    value["tracks"]["microphone"]["file"] = "audio/microphone.wav"
    value["tracks"]["system_audio"]["file"] = "audio/system_audio.wav"
    manifest_path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(TranscriptionError, match="音声ファイル名が不正"):
        TranscriptionService(_Backend(), model_name="test-model").run(session)


def test_transcription_rejects_legacy_session_schema(tmp_path: Path) -> None:
    session = _session(tmp_path)
    (session / "session.json").write_text(
        json.dumps({"schema_version": 1, "status": "RECORDED"}),
        encoding="utf-8",
    )

    with pytest.raises(TranscriptionError, match="現在のデータ形式"):
        TranscriptionService(_Backend(), model_name="test-model").run(session)


def test_transcription_requires_at_least_one_supported_track(tmp_path: Path) -> None:
    session = _session(tmp_path)
    (session / "audio" / "manifest.json").write_text(
        json.dumps({"schema_version": 3, "tracks": {}}),
        encoding="utf-8",
    )

    with pytest.raises(TranscriptionError, match="文字起こし可能な音声"):
        TranscriptionService(_Backend(), model_name="test-model").run(session)


def test_faster_whisper_retries_cuda_runtime_failure_on_cpu(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = FasterWhisperBackend(model_name="test-model", models_directory=tmp_path)
    calls = 0
    expected = BackendTranscription((), "ja", 1.0, 0.0)

    def transcribe(_audio_path: Path, *, language: str, progress_callback=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            backend._device = "cuda"
            raise RuntimeError("Library cublas64_12.dll is not found")
        assert language == "ja"
        return expected

    monkeypatch.setattr(backend, "_transcribe_with_model", transcribe)

    result = backend.transcribe(tmp_path / "audio.wav", language="ja")

    assert result is expected
    assert calls == 2
    assert backend._force_cpu


def test_cuda_runtime_error_detection_does_not_hide_regular_inference_errors() -> None:
    assert _is_cuda_runtime_error(RuntimeError("cudnn library cannot be loaded"))
    assert not _is_cuda_runtime_error(RuntimeError("invalid audio tensor"))
