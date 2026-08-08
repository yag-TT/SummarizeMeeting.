import json
from pathlib import Path

from summarize_meeting.domain.session import RecordingSession
from summarize_meeting.infrastructure.session_repository import (
    FileSessionRepository,
    sanitize_title,
)


def test_sanitize_title_handles_windows_reserved_names() -> None:
    assert sanitize_title(" CON ") == "_CON"
    assert sanitize_title("開発:/定例?*") == "開発__定例__"


def test_repository_creates_expected_structure(tmp_path: Path) -> None:
    repository = FileSessionRepository(tmp_path)
    session = RecordingSession(title="開発定例")

    paths = repository.create(session)

    assert paths.audio.is_dir()
    assert paths.screenshots.is_dir()
    assert paths.analysis.is_dir()
    assert paths.output.is_dir()
    saved = json.loads(paths.session_json.read_text(encoding="utf-8"))
    assert saved["id"] == session.id
    assert saved["title"] == "開発定例"
