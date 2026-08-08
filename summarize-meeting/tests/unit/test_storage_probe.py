from pathlib import Path
from types import SimpleNamespace

from summarize_meeting.infrastructure import storage_probe
from summarize_meeting.infrastructure.storage_probe import SystemStorageProbe


def test_system_probe_uses_nearest_existing_parent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    checked_paths = []

    def fake_disk_usage(path: Path):
        checked_paths.append(path)
        return SimpleNamespace(free=123)

    monkeypatch.setattr(storage_probe.shutil, "disk_usage", fake_disk_usage)

    free_bytes = SystemStorageProbe().free_bytes(tmp_path / "data" / "meetings")

    assert free_bytes == 123
    assert checked_paths == [tmp_path]
