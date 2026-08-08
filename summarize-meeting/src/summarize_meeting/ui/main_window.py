from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QCheckBox,
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

from summarize_meeting.application.audio_enhancement_controller import (
    AudioEnhancementController,
)
from summarize_meeting.application.diarization_controller import DiarizationController
from summarize_meeting.application.minutes_controller import MinutesController
from summarize_meeting.application.recording_controller import (
    CaptureSourcesSnapshot,
    RecordingController,
)
from summarize_meeting.application.screen_analysis_controller import (
    ScreenAnalysisController,
)
from summarize_meeting.application.transcription_controller import TranscriptionController
from summarize_meeting.domain.capture import AudioDevice, ScreenTarget
from summarize_meeting.infrastructure.session_catalog import (
    FileSessionCatalog,
    SessionSummary,
)
from summarize_meeting.ui.status_row import CaptureStatusRow


class MainWindow(QMainWindow):
    _SOURCE_REFRESH_TIMEOUT_MS = 10_000

    def __init__(
        self,
        controller: RecordingController,
        transcription_controller: TranscriptionController | None = None,
        session_catalog: FileSessionCatalog | None = None,
        diarization_controller: DiarizationController | None = None,
        screen_analysis_controller: ScreenAnalysisController | None = None,
        minutes_controller: MinutesController | None = None,
        audio_enhancement_controller: AudioEnhancementController | None = None,
    ) -> None:
        super().__init__()
        self._controller = controller
        self._transcription_controller = transcription_controller
        self._diarization_controller = diarization_controller
        self._screen_analysis_controller = screen_analysis_controller
        self._minutes_controller = minutes_controller
        self._audio_enhancement_controller = audio_enhancement_controller
        self._session_catalog = session_catalog or FileSessionCatalog(controller.meetings_directory)
        self._started_at: datetime | None = None
        self._session_path: Path | None = None
        self._sources_loaded = False
        self._source_refresh_request_id = 0
        self._source_refresh_pending = False
        self._close_requested = False
        self._os_shutdown_requested = False
        self._session_error_message: str | None = None
        self._pending_auto_transcription_path: Path | None = None
        self.setWindowTitle("Summarize Meeting - Phase 5 PoC")
        self.resize(880, 900)

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

        analysis_selector = QHBoxLayout()
        analysis_selector.addWidget(QLabel("解析対象"))
        self._analysis_session = QComboBox()
        self._analysis_session.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self._analysis_session.setMinimumContentsLength(50)
        analysis_selector.addWidget(self._analysis_session, 1)
        self._refresh_sessions = QPushButton("会議一覧を更新")
        analysis_selector.addWidget(self._refresh_sessions)
        root.addLayout(analysis_selector)

        self._auto_transcribe = QCheckBox("録音終了後に自動で文字起こし")
        self._auto_transcribe.setChecked(controller.auto_transcribe_after_recording)
        root.addWidget(self._auto_transcribe)

        audio_enhancement = QHBoxLayout()
        audio_enhancement.addWidget(QLabel("マイク音声改善"))
        self._audio_enhancement_status = QLabel("未実行")
        audio_enhancement.addWidget(self._audio_enhancement_status)
        audio_enhancement.addStretch(1)
        self._enhance_audio = QPushButton("実行")
        self._enhance_audio.setEnabled(False)
        audio_enhancement.addWidget(self._enhance_audio)
        root.addLayout(audio_enhancement)
        self._audio_enhancement_progress = QProgressBar()
        self._audio_enhancement_progress.setRange(0, 100)
        self._audio_enhancement_progress.setValue(0)
        self._audio_enhancement_progress.setFormat("マイク音声改善 %p%")
        self._audio_enhancement_progress.setVisible(False)
        root.addWidget(self._audio_enhancement_progress)

        analysis = QHBoxLayout()
        analysis.addWidget(QLabel("文字起こし"))
        self._transcription_status = QLabel("未実行")
        analysis.addWidget(self._transcription_status)
        analysis.addStretch(1)
        self._transcribe = QPushButton("実行")
        self._transcribe.setEnabled(False)
        analysis.addWidget(self._transcribe)
        root.addLayout(analysis)
        self._transcription_progress = QProgressBar()
        self._transcription_progress.setRange(0, 100)
        self._transcription_progress.setValue(0)
        self._transcription_progress.setFormat("文字起こし %p%")
        self._transcription_progress.setVisible(False)
        root.addWidget(self._transcription_progress)

        diarization = QHBoxLayout()
        diarization.addWidget(QLabel("話者分離"))
        self._diarization_status = QLabel("未実行")
        diarization.addWidget(self._diarization_status)
        diarization.addStretch(1)
        diarization.addWidget(QLabel("話者数"))
        self._speaker_count = QComboBox()
        self._speaker_count.addItem("自動", None)
        for count in range(1, 11):
            self._speaker_count.addItem(f"{count}人", count)
        diarization.addWidget(self._speaker_count)
        self._diarize = QPushButton("実行")
        self._diarize.setEnabled(False)
        diarization.addWidget(self._diarize)
        root.addLayout(diarization)
        self._diarization_progress = QProgressBar()
        self._diarization_progress.setRange(0, 100)
        self._diarization_progress.setValue(0)
        self._diarization_progress.setFormat("話者分離 %p%")
        self._diarization_progress.setVisible(False)
        root.addWidget(self._diarization_progress)
        reset_note = QLabel("再実行すると保存済みの話者名は既定名へ戻ります。")
        reset_note.setStyleSheet("color: #666666;")
        root.addWidget(reset_note)

        self._speaker_names_widget = QWidget()
        self._speaker_names_layout = QFormLayout(self._speaker_names_widget)
        self._speaker_name_inputs: dict[str, QLineEdit] = {}
        root.addWidget(self._speaker_names_widget)
        self._save_speaker_names = QPushButton("話者名を保存")
        self._save_speaker_names.setVisible(False)
        root.addWidget(self._save_speaker_names)

        screen_analysis = QHBoxLayout()
        screen_analysis.addWidget(QLabel("画面解析"))
        self._screen_analysis_status = QLabel("未実行")
        screen_analysis.addWidget(self._screen_analysis_status)
        screen_analysis.addStretch(1)
        self._analyze_screens = QPushButton("実行")
        self._analyze_screens.setEnabled(False)
        screen_analysis.addWidget(self._analyze_screens)
        root.addLayout(screen_analysis)
        self._screen_analysis_progress = QProgressBar()
        self._screen_analysis_progress.setRange(0, 100)
        self._screen_analysis_progress.setValue(0)
        self._screen_analysis_progress.setFormat("画面解析 %p%")
        self._screen_analysis_progress.setVisible(False)
        root.addWidget(self._screen_analysis_progress)

        minutes = QHBoxLayout()
        minutes.addWidget(QLabel("議事録生成"))
        self._minutes_status = QLabel("未実行")
        minutes.addWidget(self._minutes_status)
        minutes.addStretch(1)
        self._generate_minutes = QPushButton("実行")
        self._generate_minutes.setEnabled(False)
        minutes.addWidget(self._generate_minutes)
        root.addLayout(minutes)
        self._minutes_progress = QProgressBar()
        self._minutes_progress.setRange(0, 100)
        self._minutes_progress.setValue(0)
        self._minutes_progress.setFormat("議事録生成 %p%")
        self._minutes_progress.setVisible(False)
        root.addWidget(self._minutes_progress)

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
        self._source_refresh_timeout_timer = QTimer(self)
        self._source_refresh_timeout_timer.setSingleShot(True)
        self._source_refresh_timeout_timer.timeout.connect(self._on_source_refresh_timeout)
        self._refresh.clicked.connect(self.refresh_sources)
        self._start.clicked.connect(self._start_recording)
        self._stop.clicked.connect(self._stop_recording)
        self._reselect.clicked.connect(self._replace_screen)
        self._enhance_audio.clicked.connect(self._toggle_audio_enhancement)
        self._transcribe.clicked.connect(self._toggle_transcription)
        self._diarize.clicked.connect(self._toggle_diarization)
        self._save_speaker_names.clicked.connect(self._update_speaker_names)
        self._analyze_screens.clicked.connect(self._toggle_screen_analysis)
        self._generate_minutes.clicked.connect(self._toggle_minutes)
        self._refresh_sessions.clicked.connect(lambda: self.refresh_analysis_sessions())
        self._analysis_session.currentIndexChanged.connect(self._on_analysis_session_changed)
        self._auto_transcribe.toggled.connect(self._on_auto_transcription_toggled)
        self._microphone.currentIndexChanged.connect(self._update_idle_source_names)
        self._system_audio.currentIndexChanged.connect(self._update_idle_source_names)
        self._screen_target.currentIndexChanged.connect(self._update_idle_source_names)
        controller.component_changed.connect(self._on_component_changed)
        controller.meter_changed.connect(self._on_meter_changed)
        controller.screenshot_count_changed.connect(
            lambda count: self._screenshots.setText(f"保存画像 {count}")
        )
        controller.sources_refreshed.connect(self._on_sources_refreshed)
        controller.session_preparing.connect(self._on_session_preparing)
        controller.session_started.connect(self._on_session_started)
        controller.session_start_failed.connect(self._on_session_start_failed)
        controller.session_start_cancelled.connect(self._on_session_start_cancelled)
        controller.finalize_progress.connect(self._on_finalize_progress)
        controller.session_finished.connect(self._on_session_finished)
        controller.fatal_error.connect(self._on_fatal_error)
        if transcription_controller is not None:
            transcription_controller.job_started.connect(self._on_transcription_started)
            transcription_controller.job_progress.connect(self._on_transcription_progress)
            transcription_controller.job_finished.connect(self._on_transcription_finished)
            transcription_controller.job_failed.connect(self._on_transcription_failed)
            transcription_controller.job_canceled.connect(self._on_transcription_canceled)
        if audio_enhancement_controller is not None:
            audio_enhancement_controller.job_started.connect(
                self._on_audio_enhancement_started
            )
            audio_enhancement_controller.job_progress.connect(
                self._on_audio_enhancement_progress
            )
            audio_enhancement_controller.job_finished.connect(
                self._on_audio_enhancement_finished
            )
            audio_enhancement_controller.job_failed.connect(
                self._on_audio_enhancement_failed
            )
            audio_enhancement_controller.job_canceled.connect(
                self._on_audio_enhancement_canceled
            )
        if diarization_controller is not None:
            diarization_controller.job_started.connect(self._on_diarization_started)
            diarization_controller.job_progress.connect(self._on_diarization_progress)
            diarization_controller.job_finished.connect(self._on_diarization_finished)
            diarization_controller.job_failed.connect(self._on_diarization_failed)
            diarization_controller.job_canceled.connect(self._on_diarization_canceled)
        if screen_analysis_controller is not None:
            screen_analysis_controller.job_started.connect(self._on_screen_analysis_started)
            screen_analysis_controller.job_progress.connect(self._on_screen_analysis_progress)
            screen_analysis_controller.job_finished.connect(self._on_screen_analysis_finished)
            screen_analysis_controller.job_failed.connect(self._on_screen_analysis_failed)
            screen_analysis_controller.job_canceled.connect(self._on_screen_analysis_canceled)
        if minutes_controller is not None:
            minutes_controller.job_started.connect(self._on_minutes_started)
            minutes_controller.job_progress.connect(self._on_minutes_progress)
            minutes_controller.job_finished.connect(self._on_minutes_finished)
            minutes_controller.job_failed.connect(self._on_minutes_failed)
            minutes_controller.job_canceled.connect(self._on_minutes_canceled)
        QTimer.singleShot(0, self.refresh_sources)
        QTimer.singleShot(0, self.refresh_analysis_sessions)

    def refresh_sources(self) -> None:
        self._source_refresh_request_id += 1
        request_id = self._source_refresh_request_id
        self._source_refresh_pending = True
        self._refresh.setEnabled(False)
        if not self._controller.is_recording:
            self._start.setEnabled(False)
        self.show_information("デバイス・ウィンドウ一覧を更新しています。")
        try:
            self._controller.refresh_sources_async(request_id)
            if self._source_refresh_pending and request_id == self._source_refresh_request_id:
                self._source_refresh_timeout_timer.start(self._SOURCE_REFRESH_TIMEOUT_MS)
        except Exception as exc:
            self._source_refresh_pending = False
            self._refresh.setEnabled(True)
            if not self._controller.is_recording:
                self._start.setEnabled(True)
            self.show_error(f"デバイス一覧の更新を開始できません: {exc}")

    def _on_sources_refreshed(self, request_id: int, value: object) -> None:
        if request_id != self._source_refresh_request_id:
            return
        if not isinstance(value, CaptureSourcesSnapshot):
            self.show_error("デバイス一覧の更新結果を読み取れませんでした。")
            self._restore_after_source_refresh()
            return
        missing_devices: list[str] = []
        microphone_found = self._populate_audio_combo(
            self._microphone,
            value.microphones,
            "マイクなし",
            preferred_id=(
                self._controller.last_microphone_device_id if not self._sources_loaded else None
            ),
        )
        if not microphone_found:
            missing_devices.append("前回のマイク")
        system_audio_found = self._populate_audio_combo(
            self._system_audio,
            value.system_audio,
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
        for target in value.screens:
            self._screen_target.addItem(target.title, target)
            if target.id == selected_screen_id:
                self._screen_target.setCurrentIndex(self._screen_target.count() - 1)
        if not value.errors:
            self._sources_loaded = True
        self._update_idle_source_names()
        self._restore_after_source_refresh()
        if value.errors:
            self.show_error(" / ".join(value.errors))
            return
        message = "PC音声は選択した出力デバイスから再生される全音声を記録します。"
        if missing_devices:
            missing = "、".join(missing_devices)
            message += f" {missing}が見つからないため再選択してください。"
        self.show_information(message)

    def _restore_after_source_refresh(self) -> None:
        self._source_refresh_timeout_timer.stop()
        self._source_refresh_pending = False
        can_edit = not self._controller.is_recording
        actively_recording = self._started_at is not None and self._stop.isEnabled()
        self._refresh.setEnabled(can_edit or actively_recording)
        if can_edit:
            self._start.setEnabled(True)

    def _on_source_refresh_timeout(self, request_id: int | None = None) -> None:
        request_id = self._source_refresh_request_id if request_id is None else request_id
        if request_id != self._source_refresh_request_id or not self._source_refresh_pending:
            return
        self._source_refresh_request_id += 1
        self._restore_after_source_refresh()
        self.show_error(
            "デバイス・ウィンドウ一覧の取得が10秒以内に完了しませんでした。"
            "接続を確認して更新を再試行してください。"
        )

    def _populate_audio_combo(
        self,
        combo: QComboBox,
        devices: Sequence[AudioDevice],
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
        self._screen_status.set_source(target.title)

    def _on_session_preparing(self, path: str) -> None:
        self._session_path = Path(path)
        self._session_error_message = None
        self._show_save_path(self._session_path)
        self._show_active_source_names()
        self._started_at = None
        self._timer.stop()
        self._set_inputs_enabled(False)
        self._start.setEnabled(False)
        self._stop.setEnabled(True)
        self._reselect.setEnabled(False)
        self._screenshots.setText("保存画像 0")
        self._audio_enhancement_status.setText("未実行")
        self._enhance_audio.setEnabled(False)
        self._transcription_status.setText("未実行")
        self._transcribe.setEnabled(False)
        self._diarization_status.setText("未実行")
        self._diarize.setEnabled(False)
        self._screen_analysis_status.setText("未実行")
        self._analyze_screens.setEnabled(False)
        self._minutes_status.setText("未実行")
        self._generate_minutes.setEnabled(False)
        self._clear_speaker_names()
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
        error_message = self._session_error_message
        self._reset_after_session()
        if error_message:
            self.show_error(f"記録の保存中に問題が発生しました: {error_message} 保存先: {path}")
        else:
            self.show_information(f"記録を保存しました: {path}")
        self.refresh_analysis_sessions(Path(path))
        self._maybe_start_auto_audio_enhancement(Path(path), error_message=error_message)
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
        self._session_error_message = None
        self._on_analysis_session_changed()

    def _toggle_transcription(self) -> None:
        controller = self._transcription_controller
        if controller is None:
            self.show_error("文字起こし機能を初期化できませんでした。")
            return
        if controller.is_running:
            self._transcribe.setEnabled(False)
            self._transcription_status.setText("キャンセル中")
            controller.cancel()
            return
        summary = self._selected_analysis_session()
        if summary is None:
            self.show_error("文字起こしする会議記録がありません。")
            return
        try:
            controller.start(summary.path)
        except Exception as exc:
            self.show_error(str(exc))

    def _toggle_audio_enhancement(self) -> None:
        controller = self._audio_enhancement_controller
        if controller is None:
            self.show_error("マイク音声改善機能を初期化できませんでした。")
            return
        if controller.is_running:
            self._enhance_audio.setEnabled(False)
            self._audio_enhancement_status.setText("キャンセル中")
            controller.cancel()
            return
        summary = self._selected_analysis_session()
        if summary is None:
            self.show_error("改善するマイク音声がありません。")
            return
        self._pending_auto_transcription_path = None
        try:
            controller.start(summary.path)
        except Exception as exc:
            self.show_error(str(exc))

    def _toggle_diarization(self) -> None:
        controller = self._diarization_controller
        if controller is None:
            self.show_error("話者分離機能を初期化できませんでした。")
            return
        if controller.is_running:
            self._diarize.setEnabled(False)
            self._diarization_status.setText("キャンセル中")
            controller.cancel()
            return
        summary = self._selected_analysis_session()
        if summary is None:
            self.show_error("話者分離する会議記録がありません。")
            return
        try:
            controller.start(summary.path, speaker_count=self._speaker_count.currentData())
        except Exception as exc:
            self.show_error(str(exc))

    def _toggle_screen_analysis(self) -> None:
        controller = self._screen_analysis_controller
        if controller is None:
            self.show_error("画面解析機能を初期化できませんでした。")
            return
        if controller.is_running:
            self._analyze_screens.setEnabled(False)
            self._screen_analysis_status.setText("キャンセル中")
            controller.cancel()
            return
        summary = self._selected_analysis_session()
        if summary is None:
            self.show_error("画面解析する会議記録がありません。")
            return
        try:
            controller.start(summary.path)
        except Exception as exc:
            self.show_error(str(exc))

    def _toggle_minutes(self) -> None:
        controller = self._minutes_controller
        if controller is None:
            self.show_error("議事録生成機能を初期化できませんでした。")
            return
        if controller.is_running:
            self._generate_minutes.setEnabled(False)
            self._minutes_status.setText("キャンセル中")
            controller.cancel()
            return
        summary = self._selected_analysis_session()
        if summary is None:
            self.show_error("議事録を生成する会議記録がありません。")
            return
        try:
            controller.start(summary.path)
        except Exception as exc:
            self.show_error(str(exc))

    def _update_speaker_names(self) -> None:
        controller = self._diarization_controller
        summary = self._selected_analysis_session()
        if controller is None or summary is None:
            self.show_error("話者名を保存する会議記録がありません。")
            return
        names = {
            speaker_id: editor.text() for speaker_id, editor in self._speaker_name_inputs.items()
        }
        try:
            output = controller.update_speaker_names(summary.path, names)
        except Exception as exc:
            self.show_error(str(exc))
            return
        self._load_speaker_names(summary.path)
        self.show_information(f"話者名と議事録を更新しました: {output}")

    def _maybe_start_auto_transcription(
        self,
        session_path: Path,
        *,
        error_message: str | None,
    ) -> None:
        if (
            error_message is not None
            or self._close_requested
            or self._os_shutdown_requested
            or not self._auto_transcribe.isChecked()
        ):
            return
        summary = self._selected_analysis_session()
        controller = self._transcription_controller
        if (
            summary is None
            or summary.path != session_path.resolve()
            or summary.recording_status != "RECORDED"
            or not summary.can_transcribe
            or controller is None
            or controller.is_running
        ):
            return
        self.show_information("録音を保存しました。文字起こしを自動実行します。")
        try:
            controller.start(summary.path)
        except Exception as exc:
            self.show_error(f"文字起こしを自動実行できません: {exc}")

    def _maybe_start_auto_audio_enhancement(
        self,
        session_path: Path,
        *,
        error_message: str | None,
    ) -> None:
        if (
            error_message is not None
            or self._close_requested
            or self._os_shutdown_requested
        ):
            return
        summary = self._selected_analysis_session()
        controller = self._audio_enhancement_controller
        if (
            summary is None
            or summary.path != session_path.resolve()
            or summary.recording_status != "RECORDED"
            or not summary.can_enhance_audio
        ):
            self._maybe_start_auto_transcription(session_path, error_message=None)
            return
        if controller is None or controller.is_running:
            self.show_warning(
                "マイク音声を自動改善できないため、原音で文字起こしを続行します。"
            )
            self._maybe_start_auto_transcription(session_path, error_message=None)
            return
        self._pending_auto_transcription_path = (
            session_path.resolve() if self._auto_transcribe.isChecked() else None
        )
        self.show_information("録音を保存しました。マイク音声を自動改善します。")
        try:
            controller.start(summary.path)
        except Exception as exc:
            self._pending_auto_transcription_path = None
            self.show_warning(
                f"マイク音声を自動改善できません: {exc} 原音を使用します。"
            )
            self._maybe_start_auto_transcription(session_path, error_message=None)

    def _on_auto_transcription_toggled(self, enabled: bool) -> None:
        try:
            self._controller.set_auto_transcribe_after_recording(enabled)
        except Exception as exc:
            self._auto_transcribe.blockSignals(True)
            self._auto_transcribe.setChecked(self._controller.auto_transcribe_after_recording)
            self._auto_transcribe.blockSignals(False)
            self.show_error(str(exc))

    def _on_audio_enhancement_started(self, session_path: str) -> None:
        if not self._is_current_session(session_path):
            return
        self._set_inputs_enabled(False)
        self._start.setEnabled(False)
        self._audio_enhancement_status.setText("実行中")
        self._enhance_audio.setText("キャンセル")
        self._enhance_audio.setEnabled(True)
        self._transcribe.setEnabled(False)
        self._diarize.setEnabled(False)
        self._analyze_screens.setEnabled(False)
        self._generate_minutes.setEnabled(False)
        self._audio_enhancement_progress.setValue(0)
        self._audio_enhancement_progress.setVisible(True)
        self.show_information("マイク音声のノイズと音量を改善しています。")

    def _on_audio_enhancement_progress(self, percent: int, message: str) -> None:
        self._audio_enhancement_progress.setValue(percent)
        self._audio_enhancement_progress.setFormat(f"マイク音声改善 %p% - {message}")
        self.show_information(message)

    def _on_audio_enhancement_finished(self, session_path: str, output_path: str) -> None:
        if not self._is_current_session(session_path):
            return
        auto_transcribe = self._consume_pending_auto_transcription(session_path)
        self._finish_audio_enhancement_ui("完了")
        self.refresh_analysis_sessions(Path(session_path))
        if auto_transcribe:
            self.show_information("マイク音声を改善しました。文字起こしを自動実行します。")
            self._maybe_start_auto_transcription(Path(session_path), error_message=None)
        else:
            self.show_information(
                f"改善版マイク音声を保存しました: {output_path} "
                "文字起こしへ反映するには実行または再実行してください。"
            )

    def _on_audio_enhancement_failed(self, session_path: str, message: str) -> None:
        if not self._is_current_session(session_path):
            return
        auto_transcribe = self._consume_pending_auto_transcription(session_path)
        self._finish_audio_enhancement_ui("失敗")
        if auto_transcribe:
            self._maybe_start_auto_transcription(Path(session_path), error_message=None)
            self.show_warning(
                f"{message} 原音は変更されていません。原音で文字起こしを開始します。"
            )
        else:
            self.show_warning(f"{message} 原音は変更されていません。")

    def _on_audio_enhancement_canceled(self, session_path: str) -> None:
        if not self._is_current_session(session_path):
            return
        auto_transcribe = self._consume_pending_auto_transcription(session_path)
        self._finish_audio_enhancement_ui("キャンセル")
        if auto_transcribe:
            self._maybe_start_auto_transcription(Path(session_path), error_message=None)
            self.show_warning(
                "マイク音声の改善をキャンセルしました。原音で文字起こしを開始します。"
            )
        else:
            self.show_warning(
                "マイク音声の改善をキャンセルしました。原音は変更されていません。"
            )

    def _consume_pending_auto_transcription(self, session_path: str) -> bool:
        path = Path(session_path).resolve()
        pending = self._pending_auto_transcription_path
        if pending != path:
            return False
        self._pending_auto_transcription_path = None
        return True

    def _on_transcription_started(self, session_path: str) -> None:
        if not self._is_current_session(session_path):
            return
        self._set_inputs_enabled(False)
        self._start.setEnabled(False)
        self._transcription_status.setText("実行中")
        self._transcribe.setText("キャンセル")
        self._transcribe.setEnabled(True)
        self._diarize.setEnabled(False)
        self._analyze_screens.setEnabled(False)
        self._generate_minutes.setEnabled(False)
        self._transcription_progress.setValue(0)
        self._transcription_progress.setVisible(True)
        self.show_information(
            "文字起こしモデルを準備しています。初回はモデルの取得に時間がかかります。"
        )

    def _on_transcription_progress(self, percent: int, message: str) -> None:
        self._transcription_progress.setValue(percent)
        self._transcription_progress.setFormat(f"文字起こし %p% - {message}")
        self.show_information(message)

    def _on_transcription_finished(self, session_path: str, output_path: str) -> None:
        if not self._is_current_session(session_path):
            return
        self._finish_transcription_ui("完了")
        self.refresh_analysis_sessions(Path(session_path))
        self.show_information(f"文字起こしを保存しました: {output_path}")

    def _on_transcription_failed(self, session_path: str, message: str) -> None:
        if not self._is_current_session(session_path):
            return
        self._finish_transcription_ui("失敗")
        self.show_error(message)

    def _on_transcription_canceled(self, session_path: str) -> None:
        if not self._is_current_session(session_path):
            return
        self._finish_transcription_ui("キャンセル")
        self.show_information("文字起こしをキャンセルしました。")

    def _on_diarization_started(self, session_path: str) -> None:
        if not self._is_current_session(session_path):
            return
        self._set_inputs_enabled(False)
        self._start.setEnabled(False)
        self._transcribe.setEnabled(False)
        self._analyze_screens.setEnabled(False)
        self._generate_minutes.setEnabled(False)
        self._diarization_status.setText("実行中")
        self._diarize.setText("キャンセル")
        self._diarize.setEnabled(True)
        self._diarization_progress.setValue(0)
        self._diarization_progress.setVisible(True)
        self._speaker_names_widget.setEnabled(False)
        self._save_speaker_names.setVisible(False)
        self.show_information("PC音声から話者を分離しています。")

    def _on_diarization_progress(self, percent: int, message: str) -> None:
        self._diarization_progress.setValue(percent)
        self._diarization_progress.setFormat(f"話者分離 %p% - {message}")
        self.show_information(message)

    def _on_diarization_finished(self, session_path: str, output_path: str) -> None:
        if not self._is_current_session(session_path):
            return
        self._finish_diarization_ui("完了")
        self.refresh_analysis_sessions(Path(session_path))
        self.show_information(f"話者付き文字起こしを保存しました: {output_path}")

    def _on_diarization_failed(self, session_path: str, message: str) -> None:
        if not self._is_current_session(session_path):
            return
        self._finish_diarization_ui("失敗")
        self.show_error(message)

    def _on_diarization_canceled(self, session_path: str) -> None:
        if not self._is_current_session(session_path):
            return
        self._finish_diarization_ui("キャンセル")
        self.show_information("話者分離をキャンセルしました。")

    def _on_screen_analysis_started(self, session_path: str) -> None:
        if not self._is_current_session(session_path):
            return
        self._set_inputs_enabled(False)
        self._start.setEnabled(False)
        self._transcribe.setEnabled(False)
        self._diarize.setEnabled(False)
        self._generate_minutes.setEnabled(False)
        self._screen_analysis_status.setText("実行中")
        self._analyze_screens.setText("キャンセル")
        self._analyze_screens.setEnabled(True)
        self._screen_analysis_progress.setValue(0)
        self._screen_analysis_progress.setVisible(True)
        self.show_information("保存済みスクリーンショットを解析しています。")

    def _on_screen_analysis_progress(self, percent: int, message: str) -> None:
        self._screen_analysis_progress.setValue(percent)
        self._screen_analysis_progress.setFormat(f"画面解析 %p% - {message}")
        self.show_information(message)

    def _on_screen_analysis_finished(self, session_path: str, output_path: str) -> None:
        if not self._is_current_session(session_path):
            return
        self._finish_screen_analysis_ui("完了")
        self.refresh_analysis_sessions(Path(session_path))
        self.show_information(f"画面解析結果を保存しました: {output_path}")

    def _on_screen_analysis_failed(self, session_path: str, message: str) -> None:
        if not self._is_current_session(session_path):
            return
        self._finish_screen_analysis_ui("失敗")
        self.show_error(message)

    def _on_screen_analysis_canceled(self, session_path: str) -> None:
        if not self._is_current_session(session_path):
            return
        self._finish_screen_analysis_ui("キャンセル")
        self.show_information("画面解析をキャンセルしました。")

    def _on_minutes_started(self, session_path: str) -> None:
        if not self._is_current_session(session_path):
            return
        self._set_inputs_enabled(False)
        self._start.setEnabled(False)
        self._transcribe.setEnabled(False)
        self._diarize.setEnabled(False)
        self._analyze_screens.setEnabled(False)
        self._minutes_status.setText("実行中")
        self._generate_minutes.setText("キャンセル")
        self._generate_minutes.setEnabled(True)
        self._minutes_progress.setValue(0)
        self._minutes_progress.setVisible(True)
        self.show_information("ローカルLLMで議事録を生成しています。")

    def _on_minutes_progress(self, percent: int, message: str) -> None:
        self._minutes_progress.setValue(percent)
        self._minutes_progress.setFormat(f"議事録生成 %p% - {message}")
        self.show_information(message)

    def _on_minutes_finished(self, session_path: str, output_path: str) -> None:
        if not self._is_current_session(session_path):
            return
        self._finish_minutes_ui("完了")
        self.refresh_analysis_sessions(Path(session_path))
        self.show_information(f"議事録を保存しました: {output_path}")

    def _on_minutes_failed(self, session_path: str, message: str) -> None:
        if not self._is_current_session(session_path):
            return
        self._finish_minutes_ui("失敗")
        self.show_error(message)

    def _on_minutes_canceled(self, session_path: str) -> None:
        if not self._is_current_session(session_path):
            return
        self._finish_minutes_ui("キャンセル")
        self.show_information("議事録生成をキャンセルしました。")

    def _finish_minutes_ui(self, status: str) -> None:
        self._minutes_status.setText(status)
        self._generate_minutes.setText("再実行")
        self._minutes_progress.setVisible(False)
        self._set_inputs_enabled(True)
        self._start.setEnabled(True)
        self._update_analysis_availability()

    def _finish_screen_analysis_ui(self, status: str) -> None:
        self._screen_analysis_status.setText(status)
        self._analyze_screens.setText("再実行")
        self._screen_analysis_progress.setVisible(False)
        self._set_inputs_enabled(True)
        self._start.setEnabled(True)
        self._update_analysis_availability()

    def _finish_diarization_ui(self, status: str) -> None:
        self._diarization_status.setText(status)
        self._diarize.setText("再実行")
        self._diarization_progress.setVisible(False)
        self._speaker_names_widget.setEnabled(True)
        self._set_inputs_enabled(True)
        self._start.setEnabled(True)
        self._update_analysis_availability()

    def _finish_transcription_ui(self, status: str) -> None:
        self._transcription_status.setText(status)
        self._transcribe.setText("再実行")
        self._transcribe.setEnabled(True)
        self._transcription_progress.setVisible(False)
        self._set_inputs_enabled(True)
        self._start.setEnabled(True)
        self._update_analysis_availability()

    def _finish_audio_enhancement_ui(self, status: str) -> None:
        self._audio_enhancement_status.setText(status)
        self._enhance_audio.setText("再実行")
        self._audio_enhancement_progress.setVisible(False)
        self._set_inputs_enabled(True)
        self._start.setEnabled(True)
        self._update_analysis_availability()

    def _update_analysis_availability(self) -> None:
        summary = self._selected_analysis_session()
        controllers = (
            self._audio_enhancement_controller,
            self._transcription_controller,
            self._diarization_controller,
            self._screen_analysis_controller,
            self._minutes_controller,
        )

        def other_running(current: object | None) -> bool:
            return any(
                controller is not None
                and controller is not current
                and controller.is_running
                for controller in controllers
            )

        transcription = self._transcription_controller
        self._transcribe.setEnabled(
            transcription is not None
            and summary is not None
            and (transcription.is_running or summary.can_transcribe)
            and not other_running(transcription)
        )
        audio_enhancement = self._audio_enhancement_controller
        self._enhance_audio.setEnabled(
            audio_enhancement is not None
            and summary is not None
            and (audio_enhancement.is_running or summary.can_enhance_audio)
            and not other_running(audio_enhancement)
        )
        diarization = self._diarization_controller
        self._diarize.setEnabled(
            diarization is not None
            and summary is not None
            and (diarization.is_running or summary.can_diarize)
            and not other_running(diarization)
        )
        screen_analysis = self._screen_analysis_controller
        self._analyze_screens.setEnabled(
            screen_analysis is not None
            and summary is not None
            and (screen_analysis.is_running or summary.can_analyze_screens)
            and not other_running(screen_analysis)
        )
        minutes = self._minutes_controller
        self._generate_minutes.setEnabled(
            minutes is not None
            and summary is not None
            and (minutes.is_running or summary.can_generate_minutes)
            and not other_running(minutes)
        )

    def _is_current_session(self, value: str) -> bool:
        summary = self._selected_analysis_session()
        return summary is not None and Path(value).resolve() == summary.path

    def refresh_analysis_sessions(self, preferred_path: Path | None = None) -> None:
        selected = preferred_path
        current = self._selected_analysis_session()
        if selected is None and current is not None:
            selected = current.path
        if selected is not None:
            selected = selected.resolve()
        summaries = self._session_catalog.scan()
        self._analysis_session.blockSignals(True)
        self._analysis_session.clear()
        selected_index = 0
        if not summaries:
            self._analysis_session.addItem("録音済みセッションがありません", None)
        else:
            for index, summary in enumerate(summaries):
                self._analysis_session.addItem(summary.display_label, summary)
                if summary.path == selected:
                    selected_index = index
        self._analysis_session.setCurrentIndex(selected_index)
        self._analysis_session.blockSignals(False)
        self._on_analysis_session_changed()

    def _on_analysis_session_changed(self, _index: int | None = None) -> None:
        summary = self._selected_analysis_session()
        if summary is None:
            self._audio_enhancement_status.setText("対象なし")
            self._enhance_audio.setText("実行")
            self._enhance_audio.setEnabled(False)
            self._transcription_status.setText("対象なし")
            self._transcribe.setText("実行")
            self._transcribe.setEnabled(False)
            self._diarization_status.setText("対象なし")
            self._diarize.setText("実行")
            self._diarize.setEnabled(False)
            self._screen_analysis_status.setText("対象なし")
            self._analyze_screens.setText("実行")
            self._analyze_screens.setEnabled(False)
            self._minutes_status.setText("対象なし")
            self._generate_minutes.setText("実行")
            self._generate_minutes.setEnabled(False)
            self._clear_speaker_names()
            return
        audio_enhancement_status = {
            "SUCCEEDED": "完了",
            "NOT_STARTED": "未実行",
            "UNKNOWN": "状態不明",
            "FAILED": "失敗",
            "CANCELED": "キャンセル",
            "RUNNING": "前回中断",
        }.get(summary.audio_enhancement_status, summary.audio_enhancement_status)
        self._audio_enhancement_status.setText(audio_enhancement_status)
        self._enhance_audio.setText(
            "再実行" if summary.audio_enhancement_status != "NOT_STARTED" else "実行"
        )
        status = {
            "SUCCEEDED": "完了",
            "NOT_STARTED": "未実行",
            "INCOMPLETE": "要再実行",
            "UNKNOWN": "状態不明",
            "FAILED": "失敗",
            "CANCELED": "キャンセル",
            "RUNNING": "前回中断",
        }.get(summary.transcription_status, summary.transcription_status)
        self._transcription_status.setText(status)
        self._transcribe.setText(
            "再実行" if summary.transcription_status != "NOT_STARTED" else "実行"
        )
        diarization_status = {
            "SUCCEEDED": "完了",
            "NOT_STARTED": "未実行",
            "UNKNOWN": "状態不明",
            "FAILED": "失敗",
            "CANCELED": "キャンセル",
            "RUNNING": "前回中断",
        }.get(summary.diarization_status, summary.diarization_status)
        self._diarization_status.setText(diarization_status)
        self._diarize.setText("再実行" if summary.diarization_status != "NOT_STARTED" else "実行")
        screen_analysis_status = {
            "SUCCEEDED": "完了",
            "NOT_STARTED": "未実行",
            "UNKNOWN": "状態不明",
            "FAILED": "失敗",
            "CANCELED": "キャンセル",
            "RUNNING": "前回中断",
        }.get(summary.screen_analysis_status, summary.screen_analysis_status)
        self._screen_analysis_status.setText(screen_analysis_status)
        self._analyze_screens.setText(
            "再実行" if summary.screen_analysis_status != "NOT_STARTED" else "実行"
        )
        minutes_status = {
            "SUCCEEDED": "完了",
            "NOT_STARTED": "未実行",
            "UNKNOWN": "状態不明",
            "FAILED": "失敗",
            "CANCELED": "キャンセル",
            "RUNNING": "前回中断",
        }.get(summary.minutes_status, summary.minutes_status)
        self._minutes_status.setText(minutes_status)
        self._generate_minutes.setText(
            "再実行" if summary.minutes_status != "NOT_STARTED" else "実行"
        )
        self._load_speaker_names(summary.path)
        self._update_analysis_availability()

    def _clear_speaker_names(self) -> None:
        while self._speaker_names_layout.rowCount():
            self._speaker_names_layout.removeRow(0)
        self._speaker_name_inputs.clear()
        self._save_speaker_names.setVisible(False)

    def _load_speaker_names(self, session_path: Path) -> None:
        self._clear_speaker_names()
        path = session_path / "analysis" / "speaker_names.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        names = value.get("names") if isinstance(value, dict) else None
        if not isinstance(names, dict):
            return
        for speaker_id, name in names.items():
            if not isinstance(speaker_id, str) or not isinstance(name, str):
                continue
            editor = QLineEdit(name)
            self._speaker_name_inputs[speaker_id] = editor
            self._speaker_names_layout.addRow(speaker_id, editor)
        self._save_speaker_names.setVisible(bool(self._speaker_name_inputs))

    def _selected_analysis_session(self) -> SessionSummary | None:
        value = self._analysis_session.currentData()
        return value if isinstance(value, SessionSummary) else None

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
        self._analysis_session.setEnabled(enabled)
        self._refresh_sessions.setEnabled(enabled)
        self._auto_transcribe.setEnabled(enabled)
        self._enhance_audio.setEnabled(enabled)
        self._speaker_count.setEnabled(enabled)
        self._speaker_names_widget.setEnabled(enabled)
        self._save_speaker_names.setEnabled(enabled)

    def show_information(self, message: str) -> None:
        self._message.setText(message)
        self._message.setStyleSheet("padding: 8px; background: #f2f2f2; color: #202020;")

    def show_error(self, message: str) -> None:
        self._message.setText(message)
        self._message.setStyleSheet("padding: 8px; background: #ffd9d9; color: #8a1f1f;")

    def show_warning(self, message: str) -> None:
        self._message.setText(message)
        self._message.setStyleSheet("padding: 8px; background: #fff2cc; color: #6b4f00;")

    def _on_fatal_error(self, message: str) -> None:
        if self._controller.is_recording or self._started_at is not None:
            self._session_error_message = message
        self.show_error(message)

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

    def _update_idle_source_names(self, _index: int | None = None) -> None:
        if self._controller.is_recording:
            return
        self._show_active_source_names()

    def _show_active_source_names(self) -> None:
        microphone = self._microphone.currentData()
        system_audio = self._system_audio.currentData()
        screen_target = self._screen_target.currentData()
        self._mic_status.set_source(
            microphone.name if isinstance(microphone, AudioDevice) else None
        )
        self._system_status.set_source(
            system_audio.name if isinstance(system_audio, AudioDevice) else None
        )
        self._screen_status.set_source(
            screen_target.title if isinstance(screen_target, ScreenTarget) else None
        )

    def prepare_for_os_shutdown(self) -> None:
        self._os_shutdown_requested = True
        self._set_inputs_enabled(False)
        self._start.setEnabled(False)
        self._stop.setEnabled(False)
        self._reselect.setEnabled(False)
        if self._controller.is_recording:
            self.show_information("Windowsの終了に備えて記録を保存しています。")
        if self._transcription_controller is not None:
            self._transcription_controller.cancel()
        if self._audio_enhancement_controller is not None:
            self._audio_enhancement_controller.cancel()
        if self._diarization_controller is not None:
            self._diarization_controller.cancel()
        if self._screen_analysis_controller is not None:
            self._screen_analysis_controller.cancel()
        if self._minutes_controller is not None:
            self._minutes_controller.cancel()

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
        if self._transcription_controller is not None:
            self._transcription_controller.cancel()
        if self._audio_enhancement_controller is not None:
            self._audio_enhancement_controller.cancel()
        if self._diarization_controller is not None:
            self._diarization_controller.cancel()
        if self._screen_analysis_controller is not None:
            self._screen_analysis_controller.cancel()
        if self._minutes_controller is not None:
            self._minutes_controller.cancel()
        event.accept()
