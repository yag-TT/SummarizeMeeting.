from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication

from summarize_meeting.application.recording_controller import (
    CaptureSourcesSnapshot,
    RecordingController,
)
from summarize_meeting.domain.capture import AudioDevice, ScreenTarget
from summarize_meeting.domain.session import RecordingSession
from summarize_meeting.infrastructure.paths import PortableAppPaths
from summarize_meeting.infrastructure.session_catalog import FileSessionCatalog
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
    sources_refreshed = Signal(int, object)
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
        self.replaced_screen_targets: list[ScreenTarget] = []

    def list_input_devices(self):
        return [
            AudioDevice(id="mic-1", name="Mic one", channels=1),
            AudioDevice(id="mic-2", name="Mic two", channels=2),
        ]

    def list_loopback_devices(self):
        return [AudioDevice(id="system-1", name="Speakers", channels=2, is_loopback=True)]

    def list_screen_targets(self):
        return [
            ScreenTarget(id="screen-1", title="Planning deck"),
            ScreenTarget(id="screen-2", title="Demo browser"),
        ]

    def refresh_sources_async(self, request_id: int) -> None:
        self.sources_refreshed.emit(
            request_id,
            CaptureSourcesSnapshot(
                microphones=tuple(self.list_input_devices()),
                system_audio=tuple(self.list_loopback_devices()),
                screens=tuple(self.list_screen_targets()),
            ),
        )

    def stop_session(self) -> None:
        self.stop_count += 1

    def replace_screen_target(self, target: ScreenTarget) -> None:
        self.replaced_screen_targets.append(target)


class _UiTranscriptionController(QObject):
    job_started = Signal(str)
    job_progress = Signal(int, str)
    job_finished = Signal(str, str)
    job_failed = Signal(str, str)
    job_canceled = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.is_running = False
        self.started_paths: list[Path] = []

    def start(self, path: Path) -> None:
        self.started_paths.append(path)

    def cancel(self) -> None:
        self.is_running = False


def test_ui_restores_devices_by_saved_id(qapp: QApplication) -> None:
    controller = _UiController(microphone_id="mic-2", system_id="system-1")
    window = MainWindow(controller)  # type: ignore[arg-type]

    window.refresh_sources()

    assert window._microphone.currentData().id == "mic-2"  # noqa: SLF001
    assert window._system_audio.currentData().id == "system-1"  # noqa: SLF001
    window.close()


def test_ui_selects_past_session_and_starts_transcription(
    qapp: QApplication,
    tmp_path: Path,
) -> None:
    older = tmp_path / "older"
    newer = tmp_path / "newer"
    for session, title, started_at in (
        (older, "朝会", "2026-08-08T09:00:00+09:00"),
        (newer, "設計会議", "2026-08-08T11:00:00+09:00"),
    ):
        audio = session / "audio"
        audio.mkdir(parents=True)
        (session / "analysis").mkdir()
        (session / "output").mkdir()
        (session / "session.json").write_text(
            f'{{"title":"{title}","started_at":"{started_at}","status":"RECORDED"}}',
            encoding="utf-8",
        )
        (audio / "manifest.json").write_text('{"tracks":{}}', encoding="utf-8")
        (audio / "microphone.wav").write_bytes(b"wave")
    (newer / "analysis" / "transcription.json").write_text(
        '{"status":"SUCCEEDED"}', encoding="utf-8"
    )
    (newer / "output" / "transcript.md").write_text("# Transcript\n", encoding="utf-8")
    recording = _UiController(microphone_id=None, system_id=None)
    recording.meetings_directory = tmp_path
    transcription = _UiTranscriptionController()
    window = MainWindow(  # type: ignore[arg-type]
        recording,
        transcription,  # type: ignore[arg-type]
        FileSessionCatalog(tmp_path),
    )

    window.refresh_analysis_sessions()

    assert window._analysis_session.count() == 2  # noqa: SLF001
    assert window._analysis_session.currentData().path == newer.resolve()  # noqa: SLF001
    assert window._transcription_status.text() == "完了"  # noqa: SLF001
    assert window._transcribe.text() == "再実行"  # noqa: SLF001
    window._analysis_session.setCurrentIndex(1)  # noqa: SLF001
    assert window._analysis_session.currentData().path == older.resolve()  # noqa: SLF001
    assert window._transcription_status.text() == "未実行"  # noqa: SLF001

    window._toggle_transcription()  # noqa: SLF001

    assert transcription.started_paths == [older.resolve()]
    window.close()


def test_ui_shows_selected_capture_source_names_while_preparing(
    qapp: QApplication,
) -> None:
    controller = _UiController(microphone_id="mic-2", system_id="system-1")
    window = MainWindow(controller)  # type: ignore[arg-type]
    window.refresh_sources()
    window._screen_target.setCurrentIndex(1)  # noqa: SLF001

    window._on_session_preparing("C:/portable/data/meetings/session-001")  # noqa: SLF001

    assert window._mic_status._source.text() == "Mic two"  # noqa: SLF001
    assert window._system_status._source.text() == "Speakers"  # noqa: SLF001
    assert window._screen_status._source.text() == "Planning deck"  # noqa: SLF001
    window._on_session_start_cancelled(  # noqa: SLF001
        "C:/portable/data/meetings/session-001"
    )
    window.close()


def test_screen_source_name_changes_only_after_reselection_succeeds(
    qapp: QApplication,
) -> None:
    controller = _UiController(microphone_id=None, system_id=None)
    window = MainWindow(controller)  # type: ignore[arg-type]
    window.refresh_sources()
    window._screen_target.setCurrentIndex(1)  # noqa: SLF001
    window._on_session_preparing("C:/portable/data/meetings/session-001")  # noqa: SLF001
    controller.is_recording = True

    window._screen_target.setCurrentIndex(2)  # noqa: SLF001

    assert window._screen_status._source.text() == "Planning deck"  # noqa: SLF001

    window._replace_screen()  # noqa: SLF001

    assert controller.replaced_screen_targets[-1].title == "Demo browser"
    assert window._screen_status._source.text() == "Demo browser"  # noqa: SLF001
    controller.is_recording = False
    window._on_session_start_cancelled(  # noqa: SLF001
        "C:/portable/data/meetings/session-001"
    )
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


def test_ui_ignores_stale_source_refresh_result(qapp: QApplication) -> None:
    controller = _UiController(microphone_id=None, system_id=None)
    window = MainWindow(controller)  # type: ignore[arg-type]
    window.refresh_sources()
    current_microphones = [
        window._microphone.itemText(index)  # noqa: SLF001
        for index in range(window._microphone.count())  # noqa: SLF001
    ]
    current_request = window._source_refresh_request_id  # noqa: SLF001
    stale = CaptureSourcesSnapshot(
        microphones=(AudioDevice("stale", "Stale microphone", 1),),
        system_audio=(),
        screens=(),
    )

    window._on_sources_refreshed(current_request - 1, stale)  # noqa: SLF001

    assert [
        window._microphone.itemText(index)  # noqa: SLF001
        for index in range(window._microphone.count())  # noqa: SLF001
    ] == current_microphones
    window.close()


def test_ui_keeps_partial_sources_and_recovers_controls_after_refresh_error(
    qapp: QApplication,
) -> None:
    controller = _UiController(microphone_id=None, system_id=None)
    window = MainWindow(controller)  # type: ignore[arg-type]
    window._source_refresh_request_id = 5  # noqa: SLF001
    snapshot = CaptureSourcesSnapshot(
        microphones=(),
        system_audio=(AudioDevice("speaker", "Backup speakers", 2, is_loopback=True),),
        screens=(),
        errors=("マイク一覧を取得できません: unavailable",),
    )

    window._on_sources_refreshed(5, snapshot)  # noqa: SLF001

    assert window._microphone.count() == 1  # noqa: SLF001
    assert window._system_audio.count() == 2  # noqa: SLF001
    assert window._refresh.isEnabled()  # noqa: SLF001
    assert window._start.isEnabled()  # noqa: SLF001
    assert "マイク一覧" in window._message.text()  # noqa: SLF001
    window.close()


def test_ui_recovers_controls_and_invalidates_result_after_source_refresh_timeout(
    qapp: QApplication,
) -> None:
    controller = _UiController(microphone_id=None, system_id=None)
    window = MainWindow(controller)  # type: ignore[arg-type]
    window._source_refresh_request_id = 9  # noqa: SLF001
    window._source_refresh_pending = True  # noqa: SLF001
    window._refresh.setEnabled(False)  # noqa: SLF001
    window._start.setEnabled(False)  # noqa: SLF001

    window._on_source_refresh_timeout(9)  # noqa: SLF001

    assert window._source_refresh_request_id == 10  # noqa: SLF001
    assert not window._source_refresh_pending  # noqa: SLF001
    assert window._refresh.isEnabled()  # noqa: SLF001
    assert window._start.isEnabled()  # noqa: SLF001
    assert "10秒以内" in window._message.text()  # noqa: SLF001
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


def test_ui_keeps_session_error_visible_after_session_finishes(
    qapp: QApplication,
) -> None:
    controller = _UiController(microphone_id=None, system_id=None)
    window = MainWindow(controller)  # type: ignore[arg-type]
    window._started_at = datetime.now()  # noqa: SLF001

    window._on_fatal_error("最終WAVを確定できませんでした")  # noqa: SLF001
    window._on_session_finished("C:/sessions/interrupted")  # noqa: SLF001

    assert "最終WAVを確定できませんでした" in window._message.text()  # noqa: SLF001
    assert "C:/sessions/interrupted" in window._message.text()  # noqa: SLF001
    assert "#ffd9d9" in window._message.styleSheet()  # noqa: SLF001
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
