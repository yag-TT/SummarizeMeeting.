from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from summarize_meeting.application.recording_controller import RecordingController
from summarize_meeting.domain.capture import AudioDevice, ScreenTarget
from summarize_meeting.ui.status_row import CaptureStatusRow


class MainWindow(QMainWindow):
    def __init__(self, controller: RecordingController) -> None:
        super().__init__()
        self._controller = controller
        self._started_at: datetime | None = None
        self._session_path: Path | None = None
        self._sources_loaded = False
        self._close_requested = False
        self._os_shutdown_requested = False
        self.setWindowTitle("Summarize Meeting - Phase 1 PoC")
        self.resize(880, 520)

        central = QWidget()
        root = QVBoxLayout(central)
        form = QFormLayout()
        self._title = QLineEdit()
        self._title.setPlaceholderText("例: 開発チーム定例")
        self._microphone = QComboBox()
        self._system_audio = QComboBox()
        self._screen_target = QComboBox()
        self._save_path = QLineEdit(str(controller.meetings_directory))
        self._save_path.setReadOnly(True)
        self._save_path.setToolTip(str(controller.meetings_directory))
        form.addRow("会議名", self._title)
        form.addRow("マイク", self._microphone)
        form.addRow("PC音声", self._system_audio)
        form.addRow("取得画面", self._screen_target)
        form.addRow("保存先", self._save_path)
        root.addLayout(form)

        selector_buttons = QHBoxLayout()
        self._refresh = QPushButton("デバイス・ウィンドウを更新")
        self._reselect = QPushButton("録音中に画面を再選択")
        self._reselect.setEnabled(False)
        selector_buttons.addWidget(self._refresh)
        selector_buttons.addWidget(self._reselect)
        selector_buttons.addStretch(1)
        root.addLayout(selector_buttons)

        root.addSpacing(12)
        self._mic_status = CaptureStatusRow("マイク")
        self._system_status = CaptureStatusRow("PC音声")
        self._screen_status = CaptureStatusRow("画面", show_meter=False)
        root.addWidget(self._mic_status)
        root.addWidget(self._system_status)
        root.addWidget(self._screen_status)

        summary = QHBoxLayout()
        self._elapsed = QLabel("経過時間 00:00:00")
        self._screenshots = QLabel("保存画像 0")
        summary.addWidget(self._elapsed)
        summary.addSpacing(30)
        summary.addWidget(self._screenshots)
        summary.addStretch(1)
        root.addLayout(summary)

        self._message = QLabel("")
        self._message.setWordWrap(True)
        self._message.setStyleSheet("padding: 8px; background: #f2f2f2;")
        root.addWidget(self._message)

        self._finalize_progress = QProgressBar()
        self._finalize_progress.setRange(0, 100)
        self._finalize_progress.setValue(0)
        self._finalize_progress.setFormat("保存処理 %p%")
        self._finalize_progress.setVisible(False)
        root.addWidget(self._finalize_progress)

        action = QHBoxLayout()
        self._start = QPushButton("会議開始")
        self._stop = QPushButton("会議終了")
        self._stop.setEnabled(False)
        action.addStretch(1)
        action.addWidget(self._start)
        action.addWidget(self._stop)
        root.addLayout(action)
        self.setCentralWidget(central)

        self._timer = QTimer(self)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._update_elapsed)
        self._refresh.clicked.connect(self.refresh_sources)
        self._start.clicked.connect(self._start_recording)
        self._stop.clicked.connect(self._stop_recording)
        self._reselect.clicked.connect(self._replace_screen)
        controller.component_changed.connect(self._on_component_changed)
        controller.meter_changed.connect(self._on_meter_changed)
        controller.screenshot_count_changed.connect(
            lambda count: self._screenshots.setText(f"保存画像 {count}")
        )
        controller.session_preparing.connect(self._on_session_preparing)
        controller.session_started.connect(self._on_session_started)
        controller.session_start_failed.connect(self._on_session_start_failed)
        controller.session_start_cancelled.connect(self._on_session_start_cancelled)
        controller.finalize_progress.connect(self._on_finalize_progress)
        controller.session_finished.connect(self._on_session_finished)
        controller.fatal_error.connect(self.show_error)
        QTimer.singleShot(0, self.refresh_sources)

    def refresh_sources(self) -> None:
        self._refresh.setEnabled(False)
        try:
            missing_devices: list[str] = []
            microphone_found = self._populate_audio_combo(
                self._microphone,
                self._controller.list_input_devices(),
                "マイクなし",
                preferred_id=(
                    self._controller.last_microphone_device_id if not self._sources_loaded else None
                ),
            )
            if not microphone_found:
                missing_devices.append("前回のマイク")
            system_audio_found = self._populate_audio_combo(
                self._system_audio,
                self._controller.list_loopback_devices(),
                "PC音声なし",
                preferred_id=(
                    self._controller.last_system_device_id if not self._sources_loaded else None
                ),
            )
            if not system_audio_found:
                missing_devices.append("前回のPC音声デバイス")
            selected_screen_id = self._current_screen_id()
            self._screen_target.clear()
            self._screen_target.addItem("画面取得なし", None)
            for target in self._controller.list_screen_targets():
                self._screen_target.addItem(target.title, target)
                if target.id == selected_screen_id:
                    self._screen_target.setCurrentIndex(self._screen_target.count() - 1)
            self._sources_loaded = True
            message = "PC音声は選択した出力デバイスから再生される全音声を記録します。"
            if missing_devices:
                missing = "、".join(missing_devices)
                message += f" {missing}が見つからないため再選択してください。"
            self.show_information(message)
        except Exception as exc:
            self.show_error(f"デバイス一覧を取得できません: {exc}")
        finally:
            self._refresh.setEnabled(True)

    def _populate_audio_combo(
        self,
        combo: QComboBox,
        devices: list[AudioDevice],
        empty_label: str,
        *,
        preferred_id: str | None,
    ) -> bool:
        selected_id = self._current_audio_id(combo) or preferred_id
        selected_found = selected_id is None
        combo.clear()
        combo.addItem(empty_label, None)
        for device in devices:
            combo.addItem(f"{device.name} ({device.channels}ch)", device)
            if device.id == selected_id:
                combo.setCurrentIndex(combo.count() - 1)
                selected_found = True
        return selected_found

    def _start_recording(self) -> None:
        try:
            self._controller.start_session(
                title=self._title.text(),
                microphone=self._microphone.currentData(),
                system_audio=self._system_audio.currentData(),
                screen_target=self._screen_target.currentData(),
            )
        except Exception as exc:
            self.show_error(str(exc))

    def _stop_recording(self) -> None:
        self._stop.setEnabled(False)
        if self._started_at is None:
            self.show_information("録音の準備をキャンセルしています。")
        else:
            self._set_inputs_enabled(False)
            self._reselect.setEnabled(False)
            self._finalize_progress.setValue(0)
            self._finalize_progress.setVisible(True)
            self.show_information("記録を確定しています。しばらくお待ちください。")
        self._controller.stop_session()

    def _replace_screen(self) -> None:
        target = self._screen_target.currentData()
        if target is None:
            self.show_error("再選択するウィンドウを選んでください")
            return
        self._controller.replace_screen_target(target)

    def _on_session_preparing(self, path: str) -> None:
        self._session_path = Path(path)
        self._show_save_path(self._session_path)
        self._started_at = None
        self._timer.stop()
        self._set_inputs_enabled(False)
        self._start.setEnabled(False)
        self._stop.setEnabled(True)
        self._reselect.setEnabled(False)
        self._screenshots.setText("保存画像 0")
        self.show_information("録音デバイスを準備しています。")

    def _on_session_started(self, path: str) -> None:
        self._session_path = Path(path)
        self._show_save_path(self._session_path)
        self._started_at = datetime.now()
        self._timer.start()
        self._set_inputs_enabled(False)
        self._start.setEnabled(False)
        self._stop.setEnabled(True)
        self._refresh.setEnabled(True)
        self._screen_target.setEnabled(True)
        self._reselect.setEnabled(True)
        self._screenshots.setText("保存画像 0")
        self.show_information(f"記録中: {path}")

    def _on_session_finished(self, path: str) -> None:
        self._session_path = Path(path)
        self._show_save_path(self._session_path)
        self._reset_after_session()
        self.show_information(f"記録を保存しました: {path}")
        self._close_if_requested()

    def _on_finalize_progress(self, percent: int, message: str) -> None:
        self._set_inputs_enabled(False)
        self._start.setEnabled(False)
        self._stop.setEnabled(False)
        self._reselect.setEnabled(False)
        self._finalize_progress.setValue(percent)
        self._finalize_progress.setFormat(f"保存処理 %p% - {message}")
        self._finalize_progress.setVisible(True)
        self.show_information(message)

    def _on_session_start_failed(self, path: str, message: str) -> None:
        self._session_path = Path(path)
        self._show_save_path(self._session_path)
        self._reset_after_session()
        self.show_error(message)
        self._close_if_requested()

    def _on_session_start_cancelled(self, path: str) -> None:
        self._session_path = Path(path)
        self._show_save_path(self._session_path)
        self._reset_after_session()
        self.show_information("録音の開始をキャンセルしました。")
        self._close_if_requested()

    def _reset_after_session(self) -> None:
        self._timer.stop()
        self._started_at = None
        self._set_inputs_enabled(True)
        self._start.setEnabled(True)
        self._stop.setEnabled(False)
        self._reselect.setEnabled(False)
        self._finalize_progress.setVisible(False)
        self._finalize_progress.setValue(0)
        self._finalize_progress.setFormat("保存処理 %p%")

    def _close_if_requested(self) -> None:
        if self._close_requested:
            self._close_requested = False
            QTimer.singleShot(0, self.close)

    def _on_component_changed(self, component: str, state: str, detail: str) -> None:
        rows = {
            "microphone": self._mic_status,
            "system_audio": self._system_status,
            "screen": self._screen_status,
        }
        row = rows.get(component)
        if row is not None:
            row.set_state(state, detail)

    def _on_meter_changed(self, component: str, level: float) -> None:
        if component == "microphone":
            self._mic_status.set_level(level)
        elif component == "system_audio":
            self._system_status.set_level(level)

    def _update_elapsed(self) -> None:
        if self._started_at is None:
            return
        seconds = int((datetime.now() - self._started_at).total_seconds())
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        self._elapsed.setText(f"経過時間 {hours:02d}:{minutes:02d}:{seconds:02d}")

    def _set_inputs_enabled(self, enabled: bool) -> None:
        self._title.setEnabled(enabled)
        self._microphone.setEnabled(enabled)
        self._system_audio.setEnabled(enabled)
        self._refresh.setEnabled(enabled)
        self._screen_target.setEnabled(enabled)

    def show_information(self, message: str) -> None:
        self._message.setText(message)
        self._message.setStyleSheet("padding: 8px; background: #f2f2f2; color: #202020;")

    def show_error(self, message: str) -> None:
        self._message.setText(message)
        self._message.setStyleSheet("padding: 8px; background: #ffd9d9; color: #8a1f1f;")

    def _current_audio_id(self, combo: QComboBox) -> str | None:
        value = combo.currentData()
        return value.id if isinstance(value, AudioDevice) else None

    def _current_screen_id(self) -> str | None:
        value = self._screen_target.currentData()
        return value.id if isinstance(value, ScreenTarget) else None

    def _show_save_path(self, path: Path) -> None:
        value = str(path)
        self._save_path.setText(value)
        self._save_path.setToolTip(value)

    def prepare_for_os_shutdown(self) -> None:
        self._os_shutdown_requested = True
        self._set_inputs_enabled(False)
        self._start.setEnabled(False)
        self._stop.setEnabled(False)
        self._reselect.setEnabled(False)
        if self._controller.is_recording:
            self.show_information("Windowsの終了に備えて記録を保存しています。")

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self._controller.is_recording:
            if self._os_shutdown_requested:
                self._controller.stop_session()
                event.accept()
                return
            preparing = self._started_at is None
            answer = QMessageBox.question(
                self,
                "録音準備中です" if preparing else "録音中です",
                (
                    "録音の準備をキャンセルしてアプリを閉じますか？"
                    if preparing
                    else "録音を終了してアプリを閉じますか？"
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._close_requested = True
            self._controller.stop_session()
            event.ignore()
            return
        event.accept()
