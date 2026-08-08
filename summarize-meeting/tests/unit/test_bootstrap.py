from __future__ import annotations

from pathlib import Path

from summarize_meeting.bootstrap import _acquire_instance_lock
from summarize_meeting.infrastructure.paths import PortableAppPaths


def _paths(tmp_path: Path) -> PortableAppPaths:
    paths = PortableAppPaths(tmp_path)
    paths.ensure_writable()
    return paths


def test_instance_lock_rejects_second_process_for_same_app_root(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    first = _acquire_instance_lock(paths)
    assert first is not None

    second = _acquire_instance_lock(paths)

    assert second is None
    first.unlock()
    replacement = _acquire_instance_lock(paths)
    assert replacement is not None
    replacement.unlock()


def test_instance_lock_recovers_lock_owned_by_missing_process(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    seed = _acquire_instance_lock(paths)
    assert seed is not None
    lock_lines = paths.lock_file.read_text(encoding="utf-8").splitlines()
    seed.unlock()
    lock_lines[0] = "999999999"
    paths.lock_file.write_text(
        "\n".join(lock_lines) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    recovered = _acquire_instance_lock(paths)

    assert recovered is not None
    assert paths.lock_file.read_text(encoding="utf-8").splitlines()[0] != "999999999"
    recovered.unlock()
