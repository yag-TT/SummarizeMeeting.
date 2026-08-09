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
                "schema_version": 2,
                "title": title,
                "started_at": started_at,
                "status": "RECORDED",
            }
        ),
        encoding="utf-8",
    )
    (audio / "manifest.json").write_text(
        '{"schema_version":2,"tracks":{"microphone":{"file":"microphone.wav"}}}',
        encoding="utf-8",
    )
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


def test_catalog_skips_corrupt_and_legacy_sessions(tmp_path: Path) -> None:
    valid = _session(
        tmp_path,
        "valid",
        title="正常会議",
        started_at="2026-08-08T10:00:00+09:00",
    )
    corrupt = tmp_path / "corrupt"
    corrupt.mkdir()
    (corrupt / "session.json").write_text("{broken", encoding="utf-8")
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "session.json").write_text(
        '{"schema_version":1,"title":"旧形式","status":"RECORDED"}',
        encoding="utf-8",
    )

    values = FileSessionCatalog(tmp_path).scan()

    assert [value.path for value in values] == [valid.resolve()]


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


def test_catalog_enables_diarization_for_transcribed_system_track(tmp_path: Path) -> None:
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
        '{"schema_version":2,"tracks":{"system":{"file":"system.wav"}}}',
        encoding="utf-8",
    )
    (session / "analysis" / "jobs.json").write_text(
        '{"jobs":{"diarization":{"status":"FAILED"}}}', encoding="utf-8"
    )

    value = FileSessionCatalog(tmp_path).scan()[0]

    assert value.can_diarize
    assert value.diarization_status == "FAILED"


def test_catalog_does_not_accept_legacy_system_audio_track(tmp_path: Path) -> None:
    session = _session(
        tmp_path,
        "legacy-audio",
        title="旧音声形式",
        started_at="2026-08-08T10:00:00+09:00",
        transcript=True,
        transcription_json=True,
    )
    (session / "audio" / "system.wav").write_bytes(b"wave")
    (session / "audio" / "manifest.json").write_text(
        '{"schema_version":2,"tracks":{"system_audio":{"file":"system.wav"}}}',
        encoding="utf-8",
    )

    assert not FileSessionCatalog(tmp_path).scan()[0].can_diarize


def test_catalog_enables_screen_analysis_for_recorded_images(tmp_path: Path) -> None:
    session = _session(
        tmp_path,
        "screens",
        title="画面解析会議",
        started_at="2026-08-08T10:00:00+09:00",
    )
    screenshots = session / "screenshots"
    screenshots.mkdir()
    (screenshots / "events.jsonl").write_text(
        '{"sequence":1,"file":"000001.png"}\n', encoding="utf-8"
    )
    (screenshots / "000001.png").write_bytes(b"image")
    (session / "analysis" / "jobs.json").write_text(
        '{"jobs":{"screen_analysis":{"status":"FAILED"}}}', encoding="utf-8"
    )

    value = FileSessionCatalog(tmp_path).scan()[0]

    assert value.can_analyze_screens
    assert value.screen_analysis_status == "FAILED"


def test_catalog_enables_minutes_after_transcription_and_reads_status(tmp_path: Path) -> None:
    session = _session(
        tmp_path,
        "minutes",
        title="議事録会議",
        started_at="2026-08-08T10:00:00+09:00",
        transcript=True,
        transcription_json=True,
    )
    (session / "analysis" / "minutes.json").write_text(
        '{"status":"SUCCEEDED"}', encoding="utf-8"
    )

    value = FileSessionCatalog(tmp_path).scan()[0]

    assert value.can_generate_minutes
    assert value.minutes_status == "SUCCEEDED"
