from pathlib import Path
from threading import Thread

import pytest

from summarize_meeting.infrastructure.paths import (
    AppRootNotWritableError,
    PortableAppPaths,
)


def test_portable_paths_create_runtime_directories(tmp_path: Path) -> None:
    paths = PortableAppPaths(tmp_path)

    paths.ensure_writable()

    assert paths.meetings_dir.is_dir()
    assert paths.logs_dir.is_dir()
    assert paths.models_dir.is_dir()
    assert list(paths.data_dir.glob(".write-probe-*")) == []


def test_portable_paths_convert_directory_creation_failure_to_preflight_error(
    tmp_path: Path,
) -> None:
    app_root = tmp_path / "app-file"
    app_root.write_text("not a directory", encoding="utf-8")
    paths = PortableAppPaths(app_root)

    with pytest.raises(AppRootNotWritableError, match="アプリフォルダへ書き込めません"):
        paths.ensure_writable()


def test_portable_paths_allow_concurrent_preflight_without_probe_collision(
    tmp_path: Path,
) -> None:
    paths = PortableAppPaths(tmp_path)
    errors: list[Exception] = []

    def ensure() -> None:
        try:
            paths.ensure_writable()
        except Exception as exc:  # pragma: no cover - asserted through errors
            errors.append(exc)

    threads = [Thread(target=ensure) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2.0)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert list(paths.data_dir.glob(".write-probe-*")) == []


def test_portable_paths_remove_probe_after_preflight_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = PortableAppPaths(tmp_path)

    def fail_fsync(_file_descriptor: int) -> None:
        raise OSError("storage flush failed")

    monkeypatch.setattr("summarize_meeting.infrastructure.paths.os.fsync", fail_fsync)

    with pytest.raises(AppRootNotWritableError):
        paths.ensure_writable()

    assert list(paths.data_dir.glob(".write-probe-*")) == []
