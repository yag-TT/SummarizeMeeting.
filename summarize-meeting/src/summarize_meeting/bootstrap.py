"""Qtアプリケーションの依存構築、単一起動制御、終了処理をまとめる起動入口。"""

from __future__ import annotations

import logging
import platform
import sys
from logging.handlers import RotatingFileHandler
from typing import Protocol

from PySide6.QtCore import QLockFile, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from summarize_meeting.application.diarization_controller import DiarizationController
from summarize_meeting.application.minutes_controller import MinutesController
from summarize_meeting.application.recording_controller import RecordingController
from summarize_meeting.application.recovery_service import (
    RecoveryController,
    SessionRecoveryService,
)
from summarize_meeting.application.screen_analysis_controller import (
    ScreenAnalysisController,
)
from summarize_meeting.application.transcription_controller import TranscriptionController
from summarize_meeting.infrastructure.paths import AppRootNotWritableError, PortableAppPaths
from summarize_meeting.infrastructure.settings import FileSettingsRepository, SettingsLoadResult
from summarize_meeting.ui.font_support import configure_japanese_ui_font
from summarize_meeting.ui.main_window import MainWindow


class ShutdownWindowPort(Protocol):
    def prepare_for_os_shutdown(self) -> None: ...


class ShutdownControllerPort(Protocol):
    def stop_for_shutdown(self, timeout_seconds: float) -> bool: ...


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
    selected_font = configure_japanese_ui_font(app)
    if platform.system() == "Linux" and selected_font is None:
        detail = (
            "Japanese UI font is not installed.\n\n"
            "Run this command and start the application again:\n"
            "sudo apt install -y fontconfig fonts-noto-cjk"
        )
        print(detail, file=sys.stderr)
        QMessageBox.critical(None, "Missing Japanese font", detail)
        return 1
    paths = PortableAppPaths.discover()
    try:
        paths.ensure_writable()
    except AppRootNotWritableError as exc:
        QMessageBox.critical(None, "起動できません", str(exc))
        return 1

    instance_lock = _acquire_instance_lock(paths)
    if instance_lock is None:
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
    logging.getLogger(__name__).info("UI font selected family=%s", selected_font)
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
    transcription_controller = TranscriptionController(paths)
    diarization_controller = DiarizationController(paths)
    screen_analysis_controller = ScreenAnalysisController(paths)
    minutes_controller = MinutesController(
        paths,
        base_url=settings_result.settings.llm.base_url,
    )
    window = MainWindow(
        controller,
        transcription_controller,
        diarization_controller=diarization_controller,
        screen_analysis_controller=screen_analysis_controller,
        minutes_controller=minutes_controller,
    )
    recovery_controller = RecoveryController(SessionRecoveryService(paths.meetings_dir))
    recovery_controller.progress.connect(window.show_information)
    recovery_controller.finished.connect(window.show_information)
    recovery_controller.failed.connect(window.show_error)
    app.commitDataRequest.connect(lambda _manager: _handle_os_shutdown(window, controller))
    window.show()
    QTimer.singleShot(0, lambda: _offer_recovery(window, recovery_controller))
    if settings_result.error:
        QTimer.singleShot(100, lambda: _show_settings_fallback(window, settings_result))
    try:
        exit_code = app.exec()
    finally:
        instance_lock.unlock()
    logging.getLogger(__name__).info("Application stopped exit_code=%s", exit_code)
    return exit_code


def _acquire_instance_lock(paths: PortableAppPaths) -> QLockFile | None:
    instance_lock = QLockFile(str(paths.lock_file))
    instance_lock.setStaleLockTime(10_000)
    return instance_lock if instance_lock.tryLock(0) else None


def _handle_os_shutdown(
    window: ShutdownWindowPort,
    controller: ShutdownControllerPort,
) -> bool:
    window.prepare_for_os_shutdown()
    completed = controller.stop_for_shutdown(timeout_seconds=4.0)
    logging.getLogger(__name__).info(
        "OS shutdown recording finalize_completed=%s",
        completed,
    )
    return completed


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
