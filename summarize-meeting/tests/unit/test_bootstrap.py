from __future__ import annotations

import logging
import sys
import threading
import warnings
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QtMsgType

import summarize_meeting.bootstrap as bootstrap_module
from summarize_meeting.bootstrap import (
    _acquire_instance_lock,
    _configure_logging,
    _handle_os_shutdown,
    _install_runtime_logging_bridges,
    _log_qt_message,
    _log_unhandled_exception,
    _log_unhandled_thread_exception,
)
from summarize_meeting.infrastructure.paths import PortableAppPaths


def _paths(tmp_path: Path) -> PortableAppPaths:
    paths = PortableAppPaths(tmp_path)
    paths.ensure_writable()
    return paths


@pytest.fixture
def configured_logging(tmp_path: Path):
    root_logger = logging.getLogger()
    original_level = root_logger.level
    paths = _paths(tmp_path)
    _configure_logging(paths, "DEBUG")
    yield paths
    for handler in tuple(root_logger.handlers):
        if getattr(handler, "_summarize_meeting_application_log", False):
            root_logger.removeHandler(handler)
            handler.close()
    logging.captureWarnings(False)
    root_logger.setLevel(original_level)


def _flush_application_log() -> None:
    for handler in logging.getLogger().handlers:
        if getattr(handler, "_summarize_meeting_application_log", False):
            handler.flush()


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
        self.analysis_timeout_seconds: float | None = None

    def prepare_for_os_shutdown(self) -> None:
        self.prepared = True

    def wait_for_analysis_shutdown(self, timeout_seconds: float) -> bool:
        self.analysis_timeout_seconds = timeout_seconds
        return True


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
    assert window.analysis_timeout_seconds == 4.0
    assert controller.timeout_seconds == 4.0


def test_configure_logging_writes_file_redacts_app_root_and_captures_warning(
    configured_logging: PortableAppPaths,
) -> None:
    logging.getLogger("test.application").error(
        "Failed to read %s/data/meetings",
        configured_logging.app_root,
    )
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        warnings.warn("runtime warning", RuntimeWarning, stacklevel=1)
    _flush_application_log()

    content = (configured_logging.logs_dir / "application.log").read_text(encoding="utf-8")

    assert "Failed to read [REDACTED]/data/meetings" in content
    assert str(configured_logging.app_root) not in content
    assert "RuntimeWarning: runtime warning" in content
    handler = next(
        handler
        for handler in logging.getLogger().handlers
        if getattr(handler, "_summarize_meeting_application_log", False)
    )
    assert handler.maxBytes == 5 * 1024 * 1024
    assert handler.backupCount == 3


def test_install_runtime_logging_bridges_preserves_previous_qt_handler(monkeypatch) -> None:
    previous_calls: list[str] = []

    def previous_handler(_message_type, _context, message: str) -> None:
        previous_calls.append(message)

    monkeypatch.setattr(
        bootstrap_module,
        "qInstallMessageHandler",
        lambda _handler: previous_handler,
    )
    monkeypatch.setattr(bootstrap_module, "_previous_qt_message_handler", None)

    original_sys_excepthook = sys.excepthook
    original_thread_excepthook = threading.excepthook
    try:
        _install_runtime_logging_bridges()
        context = SimpleNamespace(file=None, line=0, category="test")
        _log_qt_message(QtMsgType.QtWarningMsg, context, "Qt warning")
    finally:
        sys.excepthook = original_sys_excepthook
        threading.excepthook = original_thread_excepthook

    assert previous_calls == ["Qt warning"]


def test_qt_warning_falls_back_to_stderr_when_no_previous_handler(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(bootstrap_module, "_previous_qt_message_handler", None)
    context = SimpleNamespace(file=None, line=0, category=None)

    _log_qt_message(QtMsgType.QtWarningMsg, context, "fallback warning")

    assert "Qt WARNING: fallback warning" in capsys.readouterr().err


def test_unhandled_exception_is_logged_and_forwarded(monkeypatch, caplog) -> None:
    forwarded: list[BaseException] = []
    monkeypatch.setattr(
        bootstrap_module,
        "_ORIGINAL_SYS_EXCEPTHOOK",
        lambda _type, exception, _traceback: forwarded.append(exception),
    )
    try:
        raise RuntimeError("unhandled test")
    except RuntimeError as exc:
        with caplog.at_level(logging.CRITICAL, logger="summarize_meeting.bootstrap"):
            _log_unhandled_exception(type(exc), exc, exc.__traceback__)

    assert "Unhandled exception" in caplog.text
    assert len(forwarded) == 1


def test_unhandled_thread_exception_is_logged_and_forwarded(monkeypatch, caplog) -> None:
    forwarded: list[object] = []
    monkeypatch.setattr(
        bootstrap_module,
        "_ORIGINAL_THREADING_EXCEPTHOOK",
        lambda args: forwarded.append(args),
    )
    try:
        raise RuntimeError("thread failure")
    except RuntimeError as exc:
        args = SimpleNamespace(
            exc_type=type(exc),
            exc_value=exc,
            exc_traceback=exc.__traceback__,
            thread=SimpleNamespace(name="test-worker"),
        )
        with caplog.at_level(logging.CRITICAL, logger="summarize_meeting.bootstrap"):
            _log_unhandled_thread_exception(args)

    assert "Unhandled thread exception thread=test-worker" in caplog.text
    assert forwarded == [args]
