from __future__ import annotations

import json
import wave
from pathlib import Path

import numpy as np
import pytest

from summarize_meeting.domain.diarization import BackendSpeakerTurn, SpeakerTurn
from summarize_meeting.processing.diarization import (
    DiarizationError,
    DiarizationService,
    _decode_mono_16k,
    merge_transcript_segments,
)


class _Backend:
    runtime_name = "fake-diarization"
    segmentation_model_name = "segmentation.onnx"
    embedding_model_name = "embedding.onnx"

    def __init__(self, turns: tuple[BackendSpeakerTurn, ...]) -> None:
        self.turns = turns
        self.calls: list[tuple[Path, int | None, float]] = []

    def diarize(
        self,
        audio_path: Path,
        *,
        speaker_count: int | None,
        cluster_threshold: float,
        progress_callback=None,
    ):
        self.calls.append((audio_path, speaker_count, cluster_threshold))
        if progress_callback is not None:
            progress_callback(0.5)
        return self.turns


def _write_wave(path: Path, *, sample_rate: int = 48_000, seconds: float = 3.0) -> None:
    frame_count = round(sample_rate * seconds)
    time = np.arange(frame_count, dtype=np.float32) / sample_rate
    samples = np.sin(2 * np.pi * 440 * time) * 0.1
    stereo = np.column_stack((samples, samples))
    pcm = np.clip(stereo * 32767, -32768, 32767).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(2)
        stream.setsampwidth(2)
        stream.setframerate(sample_rate)
        stream.writeframes(pcm.tobytes())


def _create_session(tmp_path: Path) -> Path:
    session = tmp_path / "meeting"
    _write_wave(session / "audio" / "system.wav")
    (session / "analysis").mkdir()
    (session / "output").mkdir()
    (session / "session.json").write_text(
        json.dumps({"schema_version": 2, "status": "RECORDED"}),
        encoding="utf-8",
    )
    (session / "audio" / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "tracks": {
                    "system": {
                        "file": "system.wav",
                        "estimated_start_offset_ms": 500,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (session / "analysis" / "transcription.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "SUCCEEDED",
                "segments": [
                    {
                        "start": 0.1,
                        "end": 0.4,
                        "source": "microphone",
                        "text": "始めます",
                    },
                    {
                        "start": 0.6,
                        "end": 1.3,
                        "source": "system",
                        "text": "最初の発話です",
                    },
                    {
                        "start": 1.5,
                        "end": 2.2,
                        "source": "system",
                        "text": "次の発話です",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return session


def test_service_generates_diarization_and_merged_transcript(tmp_path: Path) -> None:
    session = _create_session(tmp_path)
    backend = _Backend(
        (
            BackendSpeakerTurn(0.0, 0.9, 7),
            BackendSpeakerTurn(0.9, 2.0, 2),
        )
    )
    progress: list[int] = []

    output = DiarizationService(backend).run(
        session,
        speaker_count=2,
        progress_callback=lambda percent, _message: progress.append(percent),
    )

    assert output == session / "output" / "transcript.md"
    diarization = json.loads((session / "analysis" / "diarization.json").read_text("utf-8"))
    assert [speaker["id"] for speaker in diarization["speakers"]] == [
        "speaker_01",
        "speaker_02",
    ]
    assert diarization["turns"][0] == {
        "start": 0.5,
        "end": 1.4,
        "audio_start": 0.0,
        "audio_end": 0.9,
        "speaker_id": "speaker_01",
    }
    merged = json.loads((session / "analysis" / "diarized_transcription.json").read_text("utf-8"))[
        "segments"
    ]
    assert [segment["speaker_id"] for segment in merged] == [
        "self",
        "speaker_01",
        "speaker_02",
    ]
    assert "**自分**" in output.read_text("utf-8")
    assert "**Speaker 2**" in output.read_text("utf-8")
    assert backend.calls == [(session / "audio" / "system.wav", 2, 0.75)]
    assert progress[-1] == 100


def test_service_updates_speaker_names_without_backend_run(tmp_path: Path) -> None:
    session = _create_session(tmp_path)
    backend = _Backend((BackendSpeakerTurn(0.0, 2.0, 0),))
    service = DiarizationService(backend)
    service.run(session, speaker_count=1)

    output = service.update_speaker_names(session, {"speaker_01": "田中"})

    assert "**田中**" in output.read_text("utf-8")
    names = json.loads((session / "analysis" / "speaker_names.json").read_text("utf-8"))
    assert names["names"] == {"speaker_01": "田中"}
    assert len(backend.calls) == 1


def test_merge_marks_nearest_and_unknown_segments() -> None:
    turns = (SpeakerTurn(2.0, 3.0, 2.0, 3.0, "speaker_01"),)
    raw = [
        {"start": 1.5, "end": 1.7, "source": "system", "text": "near"},
        {"start": 5.0, "end": 5.2, "source": "system", "text": "far"},
    ]

    merged = merge_transcript_segments(
        raw,
        turns,
        {"speaker_01": "Speaker 1"},
        nearest_tolerance_seconds=0.75,
    )

    assert merged[0]["assignment"] == "nearest_turn"
    assert merged[0]["speaker_id"] == "speaker_01"
    assert merged[1]["assignment"] == "unknown"
    assert merged[1]["speaker_name"] == "不明"


def test_service_rejects_manifest_path_traversal(tmp_path: Path) -> None:
    session = _create_session(tmp_path)
    manifest_path = session / "audio" / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["tracks"]["system"]["file"] = "../outside.wav"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(DiarizationError, match="ファイル名が不正"):
        DiarizationService(_Backend((BackendSpeakerTurn(0, 1, 0),))).run(session)


def test_service_rejects_legacy_session_schema(tmp_path: Path) -> None:
    session = _create_session(tmp_path)
    (session / "session.json").write_text(
        json.dumps({"schema_version": 1, "status": "RECORDED"}),
        encoding="utf-8",
    )

    with pytest.raises(DiarizationError, match="現在のデータ形式"):
        DiarizationService(_Backend((BackendSpeakerTurn(0, 1, 0),))).run(session)


def test_decode_mono_16k_resamples_stereo_wave(tmp_path: Path) -> None:
    path = tmp_path / "source.wav"
    _write_wave(path, sample_rate=48_000, seconds=1.0)

    samples = _decode_mono_16k(path)

    assert samples.dtype == np.float32
    assert samples.ndim == 1
    assert len(samples) == pytest.approx(16_000, abs=32)
    assert float(np.max(np.abs(samples))) > 0.05
