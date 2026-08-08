from __future__ import annotations

from pathlib import Path

from summarize_meeting.bootstrap import _acquire_instance_lock, _handle_os_shutdown
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


class _ShutdownWindow:
    def __init__(self) -> None:
        self.prepared = False

    def prepare_for_os_shutdown(self) -> None:
        self.prepared = True


class _ShutdownController:
    def __init__(self, *, completed: bool) -> None:
        self._completed = completed
        self.timeout_seconds: float | None = None

    def stop_for_shutdown(self, timeout_seconds: float) -> bool:
        self.timeout_seconds = timeout_seconds
        return self._completed


def test_os_shutdown_prepares_ui_and_waits_for_bounded_finalize() -> None:
    window = _ShutdownWindow()
    controller = _ShutdownController(completed=False)

    completed = _handle_os_shutdown(window, controller)

    assert not completed
    assert window.prepared
    assert controller.timeout_seconds == 4.0
