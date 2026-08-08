import json
import wave
from pathlib import Path

import cv2
import numpy as np

from summarize_meeting.application.recovery_service import SessionRecoveryService


def _write_interrupted_session(root: Path) -> Path:
    session_root = root / "2026-08-08_test_deadbeef"
    work = session_root / "audio" / ".work" / "microphone"
    work.mkdir(parents=True)
    (session_root / "session.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "deadbeef",
                "title": "復旧テスト",
                "status": "RECORDING",
                "duration_ms": None,
                "warnings": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return session_root


def _write_wave_with_stale_header(path: Path) -> None:
    first = np.full((100, 2), 1000, dtype="<i2")
    second = np.full((100, 2), 2000, dtype="<i2")
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(2)
        stream.setsampwidth(2)
        stream.setframerate(8_000)
        stream.writeframes(first.tobytes())
    with path.open("ab") as stream:
        stream.write(second.tobytes())


def test_recovery_creates_copy_without_modifying_original(tmp_path: Path) -> None:
    session_root = _write_interrupted_session(tmp_path)
    segment = session_root / "audio" / ".work" / "microphone" / "000000.wav"
    _write_wave_with_stale_header(segment)
    service = SessionRecoveryService(tmp_path)

    candidates = service.scan()
    result = service.recover(candidates[0])

    assert len(result.tracks) == 1
    assert result.tracks[0].frames == 200
    with wave.open(str(segment), "rb") as original:
        assert original.getnframes() == 100
    with wave.open(str(session_root / "audio" / "microphone.recovered.wav"), "rb") as recovered:
        assert recovered.getnframes() == 200
    session = json.loads((session_root / "session.json").read_text(encoding="utf-8"))
    assert session["status"] == "INTERRUPTED"
    assert session["recovery"]["tracks"][0]["frames"] == 200
    assert service.scan() == []


def test_recovery_skips_corrupt_segment_and_keeps_valid_audio(tmp_path: Path) -> None:
    session_root = _write_interrupted_session(tmp_path)
    work = session_root / "audio" / ".work" / "microphone"
    _write_wave_with_stale_header(work / "000000.wav")
    (work / "000001.wav").write_bytes(b"not-a-wave")
    service = SessionRecoveryService(tmp_path)

    result = service.recover(service.scan()[0])

    assert result.tracks[0].recovered_segments == 1
    assert result.tracks[0].skipped_segments == 1
    assert any("000001.wav" in warning for warning in result.warnings)
    assert (work / "000001.wav").read_bytes() == b"not-a-wave"


def test_recovery_promotes_valid_screenshot_temp_and_keeps_corrupt_temp(
    tmp_path: Path,
) -> None:
    session_root = _write_interrupted_session(tmp_path)
    screenshots = session_root / "screenshots"
    screenshots.mkdir()
    success, encoded = cv2.imencode(
        ".png",
        np.full((10, 12, 3), 100, dtype=np.uint8),
    )
    assert success
    valid_temp = screenshots / "000001.png.tmp"
    valid_temp.write_bytes(encoded.tobytes())
    corrupt_temp = screenshots / "000002.png.tmp"
    corrupt_temp.write_bytes(b"not-a-png")
    service = SessionRecoveryService(tmp_path)

    result = service.recover(service.scan()[0])

    assert result.recovered_screenshots == ("screenshots/000001.png",)
    assert (screenshots / "000001.png").is_file()
    assert not valid_temp.exists()
    assert corrupt_temp.read_bytes() == b"not-a-png"
    assert any("000002.png.tmp" in warning for warning in result.warnings)
    session = json.loads((session_root / "session.json").read_text(encoding="utf-8"))
    assert session["recovery"]["screenshots"] == ["screenshots/000001.png"]
