from pathlib import Path

from summarize_meeting.infrastructure.paths import PortableAppPaths


def test_portable_paths_create_runtime_directories(tmp_path: Path) -> None:
    paths = PortableAppPaths(tmp_path)

    paths.ensure_writable()

    assert paths.meetings_dir.is_dir()
    assert paths.logs_dir.is_dir()
    assert not (paths.data_dir / ".write-probe").exists()
