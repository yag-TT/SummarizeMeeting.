from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

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
    backups = tuple(analysis.glob("jobs.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{broken"


def test_repository_preserves_updates_from_multiple_instances(tmp_path: Path) -> None:
    repositories = [FileAnalysisJobRepository() for _index in range(4)]
    states = [
        AnalysisJobState.start(job=job, model=None, language=None)
        for job in ("transcription", "diarization", "screen_analysis", "minutes")
    ]

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(repository.save, tmp_path, state)
            for repository, state in zip(repositories, states, strict=True)
        ]
        for future in futures:
            future.result()

    value = json.loads((tmp_path / "analysis" / "jobs.json").read_text(encoding="utf-8"))
    assert set(value["jobs"]) == {state.job for state in states}


def test_repository_does_not_overwrite_state_when_read_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = FileAnalysisJobRepository()
    existing = AnalysisJobState.start(job="transcription", model=None, language=None)
    path = repository.save(tmp_path, existing)
    original = path.read_bytes()
    original_read_text = Path.read_text

    def fail_jobs_read(self: Path, *args, **kwargs) -> str:
        if self == path:
            raise OSError("storage unavailable")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_jobs_read)

    with pytest.raises(OSError, match="storage unavailable"):
        repository.save(
            tmp_path,
            AnalysisJobState.start(job="minutes", model=None, language=None),
        )

    assert path.read_bytes() == original
