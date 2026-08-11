from __future__ import annotations

import os
from pathlib import Path

import pytest

from summarize_meeting.infrastructure import atomic_io
from summarize_meeting.infrastructure.atomic_io import ArtifactPublisher


def test_artifact_publisher_restores_all_previous_files_when_commit_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "analysis" / "first.json"
    second = tmp_path / "output" / "second.md"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_bytes(b"old-first")
    second.write_bytes(b"old-second")
    original_replace = os.replace

    def fail_second_stage(source: Path, target: Path) -> None:
        source_path = Path(source)
        target_path = Path(target)
        if source_path.name.endswith(".stage") and target_path == second:
            raise OSError("simulated commit failure")
        original_replace(source, target)

    monkeypatch.setattr(atomic_io.os, "replace", fail_second_stage)

    with pytest.raises(OSError, match="simulated commit failure"):
        ArtifactPublisher(tmp_path).publish({first: b"new-first", second: b"new-second"})

    assert first.read_bytes() == b"old-first"
    assert second.read_bytes() == b"old-second"
    assert not tuple(tmp_path.rglob(".*.stage"))
    assert not tuple(tmp_path.rglob(".*.backup"))


def test_artifact_publisher_rejects_output_outside_root(tmp_path: Path) -> None:
    publisher = ArtifactPublisher(tmp_path / "allowed")

    with pytest.raises(ValueError, match="フォルダ外"):
        publisher.publish({tmp_path / "outside.json": b"value"})
