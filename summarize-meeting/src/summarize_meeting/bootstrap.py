from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from PySide6.QtCore import QLockFile, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from summarize_meeting.application.recording_controller import RecordingController
from summarize_meeting.application.recovery_service import (
    RecoveryController,
    SessionRecoveryService,
)
from summarize_meeting.infrastructure.paths import AppRootNotWritableError, PortableAppPaths
from summarize_meeting.infrastructure.settings import FileSettingsRepository, SettingsLoadResult
from summarize_meeting.ui.main_window import MainWindow


def _configure_logging(paths: PortableAppPaths, log_level: str) -> None:
    handler = RotatingFileHandler(
        paths.logs_dir / "application.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.basicConfig(level=getattr(logging, log_level), handlers=[handler])


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

    settings_repository = FileSettingsRepository(paths.settings_file)
    settings_result = settings_repository.load()
    _configure_logging(paths, settings_result.settings.log_level)
    logging.getLogger(__name__).info("Application started app_root=%s", paths.app_root)
    if settings_result.error:
        logging.getLogger(__name__).warning(
            "Settings fallback backup=%s error=%s",
            settings_result.backup_path,
            settings_result.error,
        )
    controller = RecordingController(
        paths,
        settings=settings_result.settings,
        settings_repository=settings_repository,
    )
    window = MainWindow(controller)
    recovery_controller = RecoveryController(SessionRecoveryService(paths.meetings_dir))
    recovery_controller.progress.connect(window.show_information)
    recovery_controller.finished.connect(window.show_information)
    recovery_controller.failed.connect(window.show_error)
    window.show()
    QTimer.singleShot(0, lambda: _offer_recovery(window, recovery_controller))
    if settings_result.error:
        QTimer.singleShot(100, lambda: _show_settings_fallback(window, settings_result))
    exit_code = app.exec()
    logging.getLogger(__name__).info("Application stopped exit_code=%s", exit_code)
    instance_lock.unlock()
    return exit_code


def _offer_recovery(window: MainWindow, controller: RecoveryController) -> None:
    candidates = controller.scan()
    if not candidates:
        return
    titles = "\n".join(f"・{candidate.title}" for candidate in candidates[:5])
    if len(candidates) > 5:
        titles += f"\nほか {len(candidates) - 5}件"
    answer = QMessageBox.question(
        window,
        "中断セッションが見つかりました",
        "正常終了していないセッションがあります。\n"
        "元のWAV segmentを残したまま復旧コピーを作成しますか？\n\n"
        f"{titles}",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.Yes,
    )
    if answer == QMessageBox.StandardButton.Yes:
        controller.recover_all(candidates)


def _show_settings_fallback(window: MainWindow, result: SettingsLoadResult) -> None:
    if result.backup_path is not None:
        window.show_error(
            "設定ファイルが壊れていたため既定値で起動しました。"
            f"元の設定は {result.backup_path} へ退避しました。"
        )
    else:
        window.show_error(
            "設定ファイルを読み込めなかったため既定値で起動しました。"
            "元のファイルは変更していません。"
        )
