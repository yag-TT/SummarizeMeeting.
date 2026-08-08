from __future__ import annotations

import json
from pathlib import Path

from summarize_meeting.infrastructure.session_catalog import FileSessionCatalog


def _session(
    root: Path,
    name: str,
    *,
    title: str,
    started_at: str,
    transcript: bool = False,
    transcription_json: bool = False,
) -> Path:
    session = root / name
    audio = session / "audio"
    audio.mkdir(parents=True)
    (session / "analysis").mkdir()
    (session / "output").mkdir()
    (session / "session.json").write_text(
        json.dumps(
            {
                "title": title,
                "started_at": started_at,
                "status": "RECORDED",
            }
        ),
        encoding="utf-8",
    )
    (audio / "manifest.json").write_text('{"tracks": {}}', encoding="utf-8")
    (audio / "microphone.wav").write_bytes(b"wave")
    if transcription_json:
        (session / "analysis" / "transcription.json").write_text(
            '{"status": "SUCCEEDED"}',
            encoding="utf-8",
        )
    if transcript:
        (session / "output" / "transcript.md").write_text("# Transcript\n", encoding="utf-8")
    return session


def test_catalog_lists_newest_session_first_and_reports_transcription(tmp_path: Path) -> None:
    older = _session(
        tmp_path,
        "older",
        title="朝会",
        started_at="2026-08-08T09:00:00+09:00",
    )
    newer = _session(
        tmp_path,
        "newer",
        title="設計会議",
        started_at="2026-08-08T11:00:00+09:00",
        transcript=True,
        transcription_json=True,
    )

    values = FileSessionCatalog(tmp_path).scan()

    assert [value.path for value in values] == [newer.resolve(), older.resolve()]
    assert values[0].transcription_status == "SUCCEEDED"
    assert values[0].can_transcribe
    assert "設計会議" in values[0].display_label
    assert values[1].transcription_status == "NOT_STARTED"


def test_catalog_keeps_corrupt_session_without_breaking_other_entries(tmp_path: Path) -> None:
    valid = _session(
        tmp_path,
        "valid",
        title="正常会議",
        started_at="2026-08-08T10:00:00+09:00",
    )
    corrupt = tmp_path / "corrupt"
    corrupt.mkdir()
    (corrupt / "session.json").write_text("{broken", encoding="utf-8")

    values = FileSessionCatalog(tmp_path).scan()

    assert {value.path for value in values} == {valid.resolve(), corrupt.resolve()}
    corrupt_summary = next(value for value in values if value.path == corrupt.resolve())
    assert corrupt_summary.title == "corrupt"
    assert corrupt_summary.recording_status == "UNKNOWN"
    assert not corrupt_summary.can_transcribe


def test_catalog_marks_success_without_markdown_as_incomplete(tmp_path: Path) -> None:
    _session(
        tmp_path,
        "incomplete",
        title="出力不足",
        started_at="2026-08-08T10:00:00+09:00",
        transcription_json=True,
    )

    value = FileSessionCatalog(tmp_path).scan()[0]

    assert value.transcription_status == "INCOMPLETE"


def test_catalog_reads_persisted_failed_job_state(tmp_path: Path) -> None:
    session = _session(
        tmp_path,
        "failed",
        title="失敗会議",
        started_at="2026-08-08T10:00:00+09:00",
    )
    (session / "analysis" / "jobs.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "jobs": {"transcription": {"status": "FAILED"}},
            }
        ),
        encoding="utf-8",
    )

    value = FileSessionCatalog(tmp_path).scan()[0]

    assert value.transcription_status == "FAILED"
    assert "文字起こし失敗" in value.display_label


def test_catalog_enables_diarization_for_transcribed_system_audio(tmp_path: Path) -> None:
    session = _session(
        tmp_path,
        "diarization",
        title="話者分離会議",
        started_at="2026-08-08T10:00:00+09:00",
        transcript=True,
        transcription_json=True,
    )
    (session / "audio" / "system.wav").write_bytes(b"wave")
    (session / "audio" / "manifest.json").write_text(
        '{"tracks":{"system_audio":{"file":"system.wav"}}}',
        encoding="utf-8",
    )
    (session / "analysis" / "jobs.json").write_text(
        '{"jobs":{"diarization":{"status":"FAILED"}}}', encoding="utf-8"
    )

    value = FileSessionCatalog(tmp_path).scan()[0]

    assert value.can_diarize
    assert value.diarization_status == "FAILED"
