from __future__ import annotations

import json
from pathlib import Path

from summarize_meeting.domain.analysis_job import AnalysisJobState, AnalysisJobStatus
from summarize_meeting.infrastructure.analysis_job_repository import (
    FileAnalysisJobRepository,
)


def test_repository_persists_job_transitions_atomically(tmp_path: Path) -> None:
    repository = FileAnalysisJobRepository()
    state = AnalysisJobState.start(job="transcription", model="turbo", language="ja")

    path = repository.save(tmp_path, state)
    repository.save(
        tmp_path,
        state.finish(
            AnalysisJobStatus.SUCCEEDED,
            output_path="output/transcript.md",
        ),
    )

    value = json.loads(path.read_text(encoding="utf-8"))
    saved = value["jobs"]["transcription"]
    assert saved["status"] == "SUCCEEDED"
    assert saved["attempt_id"] == state.attempt_id
    assert saved["ended_at"] is not None
    assert saved["output_path"] == "output/transcript.md"
    assert not path.with_suffix(".json.tmp").exists()


def test_repository_replaces_corrupt_state_without_blocking_retry(tmp_path: Path) -> None:
    analysis = tmp_path / "analysis"
    analysis.mkdir()
    path = analysis / "jobs.json"
    path.write_text("{broken", encoding="utf-8")
    repository = FileAnalysisJobRepository()
    state = AnalysisJobState.start(job="transcription", model="turbo", language="ja")

    repository.save(tmp_path, state)

    value = json.loads(path.read_text(encoding="utf-8"))
    assert value["jobs"]["transcription"]["status"] == "RUNNING"
