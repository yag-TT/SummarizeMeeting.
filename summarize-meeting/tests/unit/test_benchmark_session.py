from __future__ import annotations

import json
import wave
from pathlib import Path

import pytest

from summarize_meeting.devtools.benchmark_session import create_repeated_audio_session


def _write_seed(path: Path) -> None:
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(1000)
        stream.writeframes(b"\x01\x00" * 100)


def test_create_repeated_audio_session_preserves_track_offset(tmp_path: Path) -> None:
    source = tmp_path / "source"
    audio = source / "audio"
    audio.mkdir(parents=True)
    _write_seed(audio / "microphone.wav")
    (audio / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "tracks": {
                    "microphone": {
                        "file": "microphone.wav",
                        "estimated_start_offset_ms": 250,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "output"

    create_repeated_audio_session(source, output, duration_seconds=1.25)

    with wave.open(str(output / "audio" / "microphone.wav"), "rb") as stream:
        assert stream.getnframes() == 1250
        assert stream.getframerate() == 1000
    manifest = json.loads((output / "audio" / "manifest.json").read_text("utf-8"))
    track = manifest["tracks"]["microphone"]
    assert track["estimated_start_offset_ms"] == 250
    assert track["capture_ended_offset_ms"] == 1500
    assert track["validated"]
    assert json.loads((output / "session.json").read_text("utf-8"))["status"] == "RECORDED"


def test_create_repeated_audio_session_refuses_existing_output(tmp_path: Path) -> None:
    output = tmp_path / "output"
    output.mkdir()

    with pytest.raises(FileExistsError):
        create_repeated_audio_session(tmp_path / "source", output, duration_seconds=1.0)


def test_create_repeated_audio_session_rejects_legacy_track_path(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    audio = source / "audio"
    audio.mkdir(parents=True)
    _write_seed(audio / "microphone.wav")
    (audio / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "tracks": {
                    "microphone": {
                        "file": "audio/microphone.wav",
                        "estimated_start_offset_ms": 0,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid track file"):
        create_repeated_audio_session(
            source,
            tmp_path / "output",
            duration_seconds=1.0,
        )
