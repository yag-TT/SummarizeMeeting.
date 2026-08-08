from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from PySide6.QtCore import QLockFile
from PySide6.QtWidgets import QApplication, QMessageBox

from summarize_meeting.application.recording_controller import RecordingController
from summarize_meeting.infrastructure.paths import AppRootNotWritableError, PortableAppPaths
from summarize_meeting.ui.main_window import MainWindow


def _configure_logging(paths: PortableAppPaths) -> None:
    handler = RotatingFileHandler(
        paths.logs_dir / "application.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Summarize Meeting")
    paths = PortableAppPaths.discover()
    try:
        paths.ensure_writable()
    except AppRootNotWritableError as exc:
        QMessageBox.critical(None, "起動できません", str(exc))
        return 1

    instance_lock = QLockFile(str(paths.lock_file))
    instance_lock.setStaleLockTime(10_000)
    if not instance_lock.tryLock(0):
        QMessageBox.warning(
            None,
            "既に起動しています",
            "このアプリフォルダのSummarize Meetingは既に起動しています。",
        )
        return 1

    _configure_logging(paths)
    logging.getLogger(__name__).info("Application started app_root=%s", paths.app_root)
    controller = RecordingController(paths)
    window = MainWindow(controller)
    window.show()
    exit_code = app.exec()
    logging.getLogger(__name__).info("Application stopped exit_code=%s", exit_code)
    instance_lock.unlock()
    return exit_code
