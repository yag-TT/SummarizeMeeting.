from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication

from summarize_meeting.application.recording_controller import RecordingController
from summarize_meeting.domain.capture import AudioDevice, ScreenTarget
from summarize_meeting.domain.session import RecordingSession
from summarize_meeting.infrastructure.paths import PortableAppPaths
from summarize_meeting.infrastructure.settings import (
    AppSettings,
    FileSettingsRepository,
    ScreenChangeSettings,
)
from summarize_meeting.ui.main_window import MainWindow


class _UiController(QObject):
    component_changed = Signal(str, str, str)
    meter_changed = Signal(str, float)
    screenshot_count_changed = Signal(int)
    session_preparing = Signal(str)
    session_started = Signal(str)
    session_start_failed = Signal(str, str)
    session_start_cancelled = Signal(str)
    finalize_progress = Signal(int, str)
    session_finished = Signal(str)
    fatal_error = Signal(str)

    def __init__(self, *, microphone_id: str | None, system_id: str | None) -> None:
        super().__init__()
        self.last_microphone_device_id = microphone_id
        self.last_system_device_id = system_id
        self.meetings_directory = Path("C:/portable/data/meetings")
        self.is_recording = False
        self.stop_count = 0

    def list_input_devices(self):
        return [
            AudioDevice(id="mic-1", name="Mic one", channels=1),
            AudioDevice(id="mic-2", name="Mic two", channels=2),
        ]

    def list_loopback_devices(self):
        return [AudioDevice(id="system-1", name="Speakers", channels=2, is_loopback=True)]

    def list_screen_targets(self):
        return []

    def stop_session(self) -> None:
        self.stop_count += 1


def test_ui_restores_devices_by_saved_id(qapp: QApplication) -> None:
    controller = _UiController(microphone_id="mic-2", system_id="system-1")
    window = MainWindow(controller)  # type: ignore[arg-type]

    window.refresh_sources()

    assert window._microphone.currentData().id == "mic-2"  # noqa: SLF001
    assert window._system_audio.currentData().id == "system-1"  # noqa: SLF001
    window.close()


def test_ui_shows_meetings_directory_then_actual_session_path(
    qapp: QApplication,
) -> None:
    controller = _UiController(microphone_id=None, system_id=None)
    window = MainWindow(controller)  # type: ignore[arg-type]

    assert window._save_path.text() == str(controller.meetings_directory)  # noqa: SLF001

    window._on_session_preparing("C:/portable/data/meetings/session-001")  # noqa: SLF001

    assert window._save_path.text() == str(  # noqa: SLF001
        Path("C:/portable/data/meetings/session-001")
    )
    assert window._save_path.toolTip() == window._save_path.text()  # noqa: SLF001
    window.close()


def test_ui_does_not_fallback_when_saved_device_is_missing(qapp: QApplication) -> None:
    controller = _UiController(microphone_id="missing", system_id=None)
    window = MainWindow(controller)  # type: ignore[arg-type]

    window.refresh_sources()

    assert window._microphone.currentData() is None  # noqa: SLF001
    assert "前回のマイクが見つからない" in window._message.text()  # noqa: SLF001
    window.close()


def test_ui_disables_inputs_while_preparing_and_restores_them_after_cancel(
    qapp: QApplication,
) -> None:
    controller = _UiController(microphone_id=None, system_id=None)
    window = MainWindow(controller)  # type: ignore[arg-type]

    window._on_session_preparing("C:/sessions/preparing")  # noqa: SLF001

    assert not window._title.isEnabled()  # noqa: SLF001
    assert not window._microphone.isEnabled()  # noqa: SLF001
    assert not window._system_audio.isEnabled()  # noqa: SLF001
    assert not window._screen_target.isEnabled()  # noqa: SLF001
    assert not window._refresh.isEnabled()  # noqa: SLF001
    assert not window._start.isEnabled()  # noqa: SLF001
    assert window._stop.isEnabled()  # noqa: SLF001
    assert "準備しています" in window._message.text()  # noqa: SLF001

    window._on_session_start_cancelled("C:/sessions/preparing")  # noqa: SLF001

    assert window._title.isEnabled()  # noqa: SLF001
    assert window._start.isEnabled()  # noqa: SLF001
    assert not window._stop.isEnabled()  # noqa: SLF001
    assert "キャンセルしました" in window._message.text()  # noqa: SLF001
    window.close()


def test_ui_shows_finalize_progress_and_hides_it_after_completion(
    qapp: QApplication,
) -> None:
    controller = _UiController(microphone_id=None, system_id=None)
    window = MainWindow(controller)  # type: ignore[arg-type]

    window._on_finalize_progress(42, "マイク: 音声ファイルを結合しています")  # noqa: SLF001

    assert not window._finalize_progress.isHidden()  # noqa: SLF001
    assert window._finalize_progress.value() == 42  # noqa: SLF001
    assert "結合しています" in window._finalize_progress.format()  # noqa: SLF001
    assert not window._title.isEnabled()  # noqa: SLF001
    assert not window._start.isEnabled()  # noqa: SLF001

    window._on_session_finished("C:/sessions/finished")  # noqa: SLF001

    assert not window._finalize_progress.isVisible()  # noqa: SLF001
    assert window._start.isEnabled()  # noqa: SLF001
    window.close()


def test_ui_accepts_os_shutdown_without_confirmation_while_recording(
    qapp: QApplication,
) -> None:
    controller = _UiController(microphone_id=None, system_id=None)
    controller.is_recording = True
    window = MainWindow(controller)  # type: ignore[arg-type]
    event = QCloseEvent()

    window.prepare_for_os_shutdown()
    window.closeEvent(event)

    assert event.isAccepted()
    assert controller.stop_count == 1
    assert "Windowsの終了" in window._message.text()  # noqa: SLF001
    controller.is_recording = False
    window.close()


def test_controller_applies_screen_settings_and_remembers_devices(tmp_path: Path) -> None:
    paths = PortableAppPaths(tmp_path)
    paths.ensure_writable()
    settings = AppSettings(
        screen_evaluation_fps=4.0,
        screen_change_thresholds=ScreenChangeSettings(pixel_diff_threshold=32),
    )
    repository = FileSettingsRepository(paths.settings_file)
    controller = RecordingController(
        paths,
        settings=settings,
        settings_repository=repository,
    )
    session_paths = controller._repository.create(RecordingSession(title="settings"))  # noqa: SLF001

    recorder = controller._create_screen_recorder(  # noqa: SLF001
        ScreenTarget(id="1", title="screen"),
        session_paths,
    )
    controller._remember_devices(  # noqa: SLF001
        AudioDevice(id="mic-2", name="Mic", channels=1),
        AudioDevice(id="system-1", name="Speakers", channels=2, is_loopback=True),
    )

    assert recorder._interval == 0.25  # noqa: SLF001
    assert recorder._detector._pixel_diff_threshold == 32  # noqa: SLF001
    saved = repository.load().settings
    assert saved.last_microphone_device_id == "mic-2"
    assert saved.last_system_device_id == "system-1"
