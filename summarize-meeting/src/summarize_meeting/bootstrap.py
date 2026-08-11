"""Qtアプリケーションの依存構築、単一起動制御、終了処理をまとめる起動入口。"""

from __future__ import annotations

import logging
import platform
import sys
import threading
from collections.abc import Callable
from logging.handlers import RotatingFileHandler
from types import TracebackType
from typing import Protocol

from PySide6.QtCore import (
    QLockFile,
    QMessageLogContext,
    QTimer,
    QtMsgType,
    qInstallMessageHandler,
)
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
from summarize_meeting.infrastructure.logging_support import (
    RedactingLogFormatter,
    register_sensitive_log_values,
)
from summarize_meeting.infrastructure.paths import AppRootNotWritableError, PortableAppPaths
from summarize_meeting.infrastructure.settings import FileSettingsRepository, SettingsLoadResult
from summarize_meeting.ui.font_support import configure_japanese_ui_font
from summarize_meeting.ui.main_window import MainWindow

_LOGGER = logging.getLogger(__name__)
_ORIGINAL_SYS_EXCEPTHOOK = sys.excepthook
_ORIGINAL_THREADING_EXCEPTHOOK = threading.excepthook
_QtMessageHandler = Callable[[QtMsgType, QMessageLogContext, str], None]
_previous_qt_message_handler: _QtMessageHandler | None = None


class ShutdownWindowPort(Protocol):
    def prepare_for_os_shutdown(self) -> None: ...


class ShutdownControllerPort(Protocol):
    def stop_for_shutdown(self, timeout_seconds: float) -> bool: ...


def _configure_logging(paths: PortableAppPaths, log_level: str) -> None:
    level = getattr(logging, log_level.upper(), logging.INFO)
    register_sensitive_log_values(paths.app_root)
    handler = RotatingFileHandler(
        paths.logs_dir / "application.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(
        RedactingLogFormatter(
            "%(asctime)s %(levelname)s %(name)s "
            "[process=%(process)d thread=%(threadName)s] %(message)s"
        )
    )
    handler.setLevel(level)
    handler._summarize_meeting_application_log = True

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    for existing in tuple(root_logger.handlers):
        if getattr(existing, "_summarize_meeting_application_log", False):
            root_logger.removeHandler(existing)
            existing.close()
    root_logger.addHandler(handler)
    logging.captureWarnings(True)


def _install_runtime_logging_bridges() -> None:
    """Python/Qtが標準エラーだけへ出す診断情報をアプリログにも転送する。"""

    global _previous_qt_message_handler

    sys.excepthook = _log_unhandled_exception
    threading.excepthook = _log_unhandled_thread_exception
    previous = qInstallMessageHandler(_log_qt_message)
    if previous is not _log_qt_message:
        _previous_qt_message_handler = previous


def _log_unhandled_exception(
    exception_type: type[BaseException],
    exception: BaseException,
    traceback: TracebackType | None,
) -> None:
    if issubclass(exception_type, (KeyboardInterrupt, SystemExit)):
        _ORIGINAL_SYS_EXCEPTHOOK(exception_type, exception, traceback)
        return
    _LOGGER.critical(
        "Unhandled exception",
        exc_info=(exception_type, exception, traceback),
    )
    _ORIGINAL_SYS_EXCEPTHOOK(exception_type, exception, traceback)


def _log_unhandled_thread_exception(args: threading.ExceptHookArgs) -> None:
    if issubclass(args.exc_type, (KeyboardInterrupt, SystemExit)):
        _ORIGINAL_THREADING_EXCEPTHOOK(args)
        return
    _LOGGER.critical(
        "Unhandled thread exception thread=%s",
        args.thread.name if args.thread is not None else "unknown",
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )
    _ORIGINAL_THREADING_EXCEPTHOOK(args)


def _log_qt_message(
    message_type: QtMsgType,
    context: QMessageLogContext,
    message: str,
) -> None:
    level = {
        QtMsgType.QtDebugMsg: logging.DEBUG,
        QtMsgType.QtInfoMsg: logging.INFO,
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg: logging.CRITICAL,
    }.get(message_type, logging.WARNING)
    location = ""
    if context.file:
        location = f" file={context.file}:{context.line}"
    category = f" category={context.category}" if context.category else ""
    logging.getLogger("qt").log(level, "Qt message%s%s: %s", category, location, message)
    if _previous_qt_message_handler is not None:
        _previous_qt_message_handler(message_type, context, message)
    elif level >= logging.WARNING:
        print(f"Qt {logging.getLevelName(level)}: {message}", file=sys.stderr)


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
        print(f"ERROR: {exc}", file=sys.stderr)
        QMessageBox.critical(None, "起動できません", str(exc))
        return 1

    instance_lock = _acquire_instance_lock(paths)
    if instance_lock is None:
        print("WARNING: Summarize Meeting is already running.", file=sys.stderr)
        QMessageBox.warning(
            None,
            "既に起動しています",
            "このアプリフォルダのSummarize Meetingは既に起動しています。",
        )
        return 1

    settings_repository = FileSettingsRepository(paths.settings_file)
    settings_result = settings_repository.load()
    _configure_logging(paths, settings_result.settings.log_level)
    _install_runtime_logging_bridges()
    _LOGGER.info("Application started app_root=%s", paths.app_root)
    _LOGGER.info("UI font selected family=%s", selected_font)
    if settings_result.error:
        _LOGGER.warning(
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
    _LOGGER.info("Application stopped exit_code=%s", exit_code)
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
    _LOGGER.info(
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
