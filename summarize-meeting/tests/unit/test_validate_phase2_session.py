from __future__ import annotations

import json
import wave
from pathlib import Path

from summarize_meeting.devtools.validate_phase2_session import validate_phase2_session


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _write_wave(path: Path, *, silent: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sample = b"\0\0" if silent else b"\0\x40"
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(sample * 800)


def _create_session(tmp_path: Path) -> Path:
    session = tmp_path / "session"
    _write_json(session / "session.json", {"status": "RECORDED"})
    tracks = {}
    for source, file_name in (
        ("microphone", "microphone.wav"),
        ("system_audio", "system.wav"),
    ):
        _write_wave(session / "audio" / file_name)
        tracks[source] = {
            "file": f"audio/{file_name}",
            "validated": True,
            "overflow_count": 0,
            "queue_pressure_count": 0,
            "gaps": [],
        }
    _write_json(session / "audio" / "manifest.json", {"tracks": tracks})
    segments = [
        {"start": 0.1, "end": 0.5, "source": "microphone", "text": "確認します"},
        {"start": 0.2, "end": 0.6, "source": "system", "text": "共有します"},
    ]
    _write_json(
        session / "analysis" / "transcription.json",
        {
            "status": "SUCCEEDED",
            "tracks": [
                {"source": "microphone", "runtime_device": "cuda"},
                {"source": "system", "runtime_device": "cuda"},
            ],
            "segments": segments,
        },
    )
    _write_json(
        session / "analysis" / "jobs.json",
        {"jobs": {"transcription": {"status": "SUCCEEDED"}}},
    )
    output = session / "output" / "transcript.md"
    output.parent.mkdir(parents=True)
    output.write_text("# Transcript\n\n## 00:00:00.100\nA\n\n## 00:00:00.200\nB\n")
    return session


def test_validate_phase2_session_accepts_complete_session(tmp_path: Path) -> None:
    session = _create_session(tmp_path)

    report = validate_phase2_session(
        session,
        expected_microphone_text="確認します",
        expected_system_text="共有します",
    )

    assert report.passed
    assert report.failures == []
    assert report.metrics["segment_count"] == 2


def test_validate_phase2_session_rejects_silent_and_invalid_timestamp(
    tmp_path: Path,
) -> None:
    session = _create_session(tmp_path)
    _write_wave(session / "audio" / "microphone.wav", silent=True)
    transcription_path = session / "analysis" / "transcription.json"
    transcription = json.loads(transcription_path.read_text("utf-8"))
    transcription["segments"][0]["end"] = -1
    _write_json(transcription_path, transcription)

    report = validate_phase2_session(session)

    assert not report.passed
    assert any("effectively silent" in failure for failure in report.failures)
    assert any("invalid timestamp" in failure for failure in report.failures)
