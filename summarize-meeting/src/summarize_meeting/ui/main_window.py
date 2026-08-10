from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QCloseEvent, QDesktopServices, QImage, QPixmap, QResizeEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
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
from summarize_meeting.ui.analysis_stage import AnalysisStageCard, AnalysisStageState
from summarize_meeting.ui.status_row import CaptureStatusRow


class _ScreenPreviewLabel(QLabel):
    def __init__(self) -> None:
        super().__init__("画面を選択してプレビューしてください。")
        self._source_pixmap: QPixmap | None = None
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(320, 180)
        self.setMaximumHeight(360)
        self.setWordWrap(True)
        self.setAccessibleName("選択画面のプレビュー")
        self.setStyleSheet(
            "background: #111418; color: #aeb4bd; "
            "border: 1px solid #46515c; border-radius: 4px; padding: 4px;"
        )

    def show_message(self, message: str) -> None:
        self._source_pixmap = None
        self.clear()
        self.setText(message)

    def show_frame(self, frame: np.ndarray) -> None:
        if frame.ndim != 3 or frame.shape[2] != 3 or frame.dtype != np.uint8:
            raise ValueError("プレビュー画像の形式が不正です")
        pixels = np.ascontiguousarray(frame)
        height, width, _channels = pixels.shape
        image = QImage(
            pixels.data,
            width,
            height,
            int(pixels.strides[0]),
            QImage.Format.Format_BGR888,
        ).copy()
        if image.isNull():
            raise ValueError("プレビュー画像を表示用に変換できません")
        self._source_pixmap = QPixmap.fromImage(image)
        self.setText("")
        self._render_pixmap()

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._render_pixmap()

    def _render_pixmap(self) -> None:
        if self._source_pixmap is None or self.width() <= 0 or self.height() <= 0:
            return
        self.setPixmap(
            self._source_pixmap.scaled(
                self.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )


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
    ) -> None:
        super().__init__()
        self._controller = controller
        self._transcription_controller = transcription_controller
        self._diarization_controller = diarization_controller
        self._screen_analysis_controller = screen_analysis_controller
        self._minutes_controller = minutes_controller
        self._session_catalog = session_catalog or FileSessionCatalog(controller.meetings_directory)
        self._started_at: datetime | None = None
        self._session_path: Path | None = None
        self._sources_loaded = False
        self._source_refresh_request_id = 0
        self._source_refresh_pending = False
        self._screen_preview_request_id = 0
        self._screen_preview_pending = False
        self._screen_preview_cancelling = False
        self._audio_preview_request_id = 0
        self._audio_preview_pending = False
        self._audio_preview_cancelling = False
        self._close_requested = False
        self._os_shutdown_requested = False
        self._session_error_message: str | None = None
        self.setWindowTitle("Summarize Meeting")
        self.resize(960, 820)
        self.setMinimumSize(720, 560)

        central = QWidget()
        shell = QVBoxLayout(central)
        shell.setContentsMargins(12, 12, 12, 12)
        shell.setSpacing(10)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget()
        root = QVBoxLayout(content)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        recording_group = QGroupBox("録音設定")
        recording_layout = QVBoxLayout(recording_group)
        form = QFormLayout()
        self._title = QLineEdit()
        self._title.setPlaceholderText("例: 開発チーム定例")
        self._title.setAccessibleName("会議名")
        self._title_error = QLabel("")
        self._title_error.setStyleSheet("color: #ffb4ab; padding-top: 2px;")
        self._title_error.setVisible(False)
        self._title_error.setAccessibleName("会議名の入力エラー")
        title_field = QWidget()
        title_layout = QVBoxLayout(title_field)
        title_layout.setContentsMargins(0, 0, 0, 0)
        title_layout.setSpacing(2)
        title_layout.addWidget(self._title)
        title_layout.addWidget(self._title_error)
        self._microphone = QComboBox()
        self._system_audio = QComboBox()
        self._screen_target = QComboBox()
        self._save_path = QLineEdit(str(controller.meetings_directory))
        self._save_path.setReadOnly(True)
        self._save_path.setToolTip(str(controller.meetings_directory))
        self._open_recordings = QPushButton("フォルダを開く")
        self._open_recordings.setToolTip("録音データの保存先を開きます")
        save_path_row = QWidget()
        save_path_layout = QHBoxLayout(save_path_row)
        save_path_layout.setContentsMargins(0, 0, 0, 0)
        save_path_layout.setSpacing(6)
        save_path_layout.addWidget(self._save_path, 1)
        save_path_layout.addWidget(self._open_recordings)
        form.addRow("会議名", title_field)
        form.addRow("マイク", self._microphone)
        form.addRow("PC音声", self._system_audio)
        form.addRow("取得画面", self._screen_target)
        form.addRow("保存先", save_path_row)
        recording_layout.addLayout(form)

        self._screen_preview = _ScreenPreviewLabel()
        recording_layout.addWidget(self._screen_preview)

        selector_buttons = QHBoxLayout()
        self._refresh = QPushButton("デバイス・ウィンドウを更新")
        self._preview_audio = QPushButton("マイク・PC音源をテスト")
        self._preview_audio.setEnabled(False)
        self._preview_screen = QPushButton("選択画面をプレビュー")
        self._preview_screen.setEnabled(False)
        self._reselect = QPushButton("録音中に画面を再選択")
        self._reselect.setEnabled(False)
        selector_buttons.addWidget(self._refresh)
        selector_buttons.addWidget(self._reselect)
        selector_buttons.addStretch(1)
        recording_layout.addLayout(selector_buttons)
        preview_buttons = QHBoxLayout()
        preview_buttons.addWidget(self._preview_audio)
        preview_buttons.addWidget(self._preview_screen)
        preview_buttons.addStretch(1)
        recording_layout.addLayout(preview_buttons)
        root.addWidget(recording_group)

        status_group = QGroupBox("録音状態")
        status_layout = QVBoxLayout(status_group)
        self._mic_status = CaptureStatusRow("マイク")
        self._system_status = CaptureStatusRow("PC音声")
        self._screen_status = CaptureStatusRow("画面", show_meter=False)
        status_layout.addWidget(self._mic_status)
        status_layout.addWidget(self._system_status)
        status_layout.addWidget(self._screen_status)

        summary = QHBoxLayout()
        self._elapsed = QLabel("経過時間 00:00:00")
        self._screenshots = QLabel("保存画像 0")
        summary.addWidget(self._elapsed)
        summary.addSpacing(30)
        summary.addWidget(self._screenshots)
        summary.addStretch(1)
        status_layout.addLayout(summary)
        root.addWidget(status_group)

        self._message = QLabel("")
        self._message.setWordWrap(True)
        self._message.setAccessibleName("アプリからのお知らせ")
        self._message.setStyleSheet(
            "padding: 10px; background: #243447; color: #e6f2ff; "
            "border: 1px solid #3f5871; border-radius: 4px;"
        )
        root.addWidget(self._message)

        self._finalize_progress = QProgressBar()
        self._finalize_progress.setRange(0, 100)
        self._finalize_progress.setValue(0)
        self._finalize_progress.setFormat("保存処理 %p%")
        self._finalize_progress.setVisible(False)

        analysis_group = QGroupBox("録音後の解析")
        analysis_layout = QVBoxLayout(analysis_group)
        analysis_selector = QHBoxLayout()
        analysis_selector.addWidget(QLabel("解析対象"))
        self._analysis_session = QComboBox()
        self._analysis_session.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self._analysis_session.setMinimumContentsLength(28)
        analysis_selector.addWidget(self._analysis_session, 1)
        self._open_session = QPushButton("フォルダを開く")
        self._open_session.setEnabled(False)
        self._open_session.setToolTip("選択中の会議データを開きます")
        analysis_selector.addWidget(self._open_session)
        self._refresh_sessions = QPushButton("会議一覧を更新")
        analysis_selector.addWidget(self._refresh_sessions)
        analysis_layout.addLayout(analysis_selector)

        self._auto_transcribe = QCheckBox("録音終了後に自動で文字起こし")
        self._auto_transcribe.setChecked(controller.auto_transcribe_after_recording)
        analysis_layout.addWidget(self._auto_transcribe)

        flow = QGridLayout()
        flow.setContentsMargins(0, 4, 0, 0)
        flow.setHorizontalSpacing(12)
        flow.setVerticalSpacing(5)
        flow.setColumnStretch(0, 1)
        flow.setColumnStretch(1, 1)

        self._save_stage = AnalysisStageCard("1", "会議終了・録音保存")
        self._recording_save_status = self._save_stage.status_label
        self._save_stage.body_layout.addWidget(self._finalize_progress)
        flow.addWidget(self._save_stage, 0, 0, 1, 2)
        flow.addWidget(
            self._flow_arrow("録音の保存完了後に文字起こしへ進みます"),
            1,
            0,
            1,
            2,
        )

        self._transcription_stage = AnalysisStageCard("2", "文字起こし")
        self._transcription_status = self._transcription_stage.status_label
        analysis = QHBoxLayout()
        analysis.addStretch(1)
        self._open_transcript = QPushButton("文字起こしを開く")
        self._open_transcript.setEnabled(False)
        analysis.addWidget(self._open_transcript)
        self._transcribe = QPushButton("文字起こしを実行")
        self._transcribe.setEnabled(False)
        analysis.addWidget(self._transcribe)
        self._transcription_stage.body_layout.addLayout(analysis)
        self._transcription_progress = QProgressBar()
        self._transcription_progress.setRange(0, 100)
        self._transcription_progress.setValue(0)
        self._transcription_progress.setFormat("文字起こし %p%")
        self._transcription_progress.setVisible(False)
        self._transcription_stage.body_layout.addWidget(self._transcription_progress)
        flow.addWidget(self._transcription_stage, 2, 0, 1, 2)
        flow.addWidget(
            self._flow_arrow("文字起こし後に任意の追加解析へ分岐します"), 3, 0, 1, 2
        )

        self._diarization_stage = AnalysisStageCard("3A", "話者分離", optional=True)
        self._diarization_status = self._diarization_stage.status_label
        diarization = QHBoxLayout()
        diarization.addWidget(QLabel("話者数"))
        self._speaker_count = QComboBox()
        self._speaker_count.addItem("自動", None)
        for count in range(1, 11):
            self._speaker_count.addItem(f"{count}人", count)
        diarization.addWidget(self._speaker_count)
        self._diarize = QPushButton("話者分離を実行")
        self._diarize.setEnabled(False)
        diarization.addWidget(self._diarize)
        diarization.addStretch(1)
        self._diarization_stage.body_layout.addLayout(diarization)
        self._diarization_progress = QProgressBar()
        self._diarization_progress.setRange(0, 100)
        self._diarization_progress.setValue(0)
        self._diarization_progress.setFormat("話者分離 %p%")
        self._diarization_progress.setVisible(False)
        self._diarization_stage.body_layout.addWidget(self._diarization_progress)
        reset_note = QLabel("再実行すると保存済みの話者名は既定名へ戻ります。")
        reset_note.setStyleSheet("color: #aeb4bd;")
        reset_note.setWordWrap(True)
        self._diarization_stage.body_layout.addWidget(reset_note)

        self._speaker_names_widget = QWidget()
        self._speaker_names_layout = QFormLayout(self._speaker_names_widget)
        self._speaker_name_inputs: dict[str, QLineEdit] = {}
        self._diarization_stage.body_layout.addWidget(self._speaker_names_widget)
        self._save_speaker_names = QPushButton("話者名を保存")
        self._save_speaker_names.setVisible(False)
        self._diarization_stage.body_layout.addWidget(self._save_speaker_names)
        flow.addWidget(self._diarization_stage, 4, 0)

        self._screen_analysis_stage = AnalysisStageCard("3B", "画面解析", optional=True)
        self._screen_analysis_status = self._screen_analysis_stage.status_label
        screen_analysis = QHBoxLayout()
        screen_analysis.addStretch(1)
        self._open_screen_analysis = QPushButton("解析結果を開く")
        self._open_screen_analysis.setEnabled(False)
        screen_analysis.addWidget(self._open_screen_analysis)
        self._analyze_screens = QPushButton("画面解析を実行")
        self._analyze_screens.setEnabled(False)
        screen_analysis.addWidget(self._analyze_screens)
        self._screen_analysis_stage.body_layout.addLayout(screen_analysis)
        self._screen_analysis_progress = QProgressBar()
        self._screen_analysis_progress.setRange(0, 100)
        self._screen_analysis_progress.setValue(0)
        self._screen_analysis_progress.setFormat("画面解析 %p%")
        self._screen_analysis_progress.setVisible(False)
        self._screen_analysis_stage.body_layout.addWidget(self._screen_analysis_progress)
        flow.addWidget(self._screen_analysis_stage, 4, 1)

        flow.addWidget(
            self._flow_arrow("話者情報を会話要約へ反映できます"), 5, 0
        )
        flow.addWidget(
            self._flow_arrow("画面情報を会話要約へ反映できます"), 5, 1
        )

        self._minutes_stage = AnalysisStageCard("4", "会話要約")
        self._minutes_status = self._minutes_stage.status_label
        minutes = QHBoxLayout()
        minutes.addStretch(1)
        self._open_minutes = QPushButton("要約を開く")
        self._open_minutes.setEnabled(False)
        minutes.addWidget(self._open_minutes)
        self._generate_minutes = QPushButton("要約を生成")
        self._generate_minutes.setEnabled(False)
        minutes.addWidget(self._generate_minutes)
        self._minutes_stage.body_layout.addLayout(minutes)
        self._minutes_progress = QProgressBar()
        self._minutes_progress.setRange(0, 100)
        self._minutes_progress.setValue(0)
        self._minutes_progress.setFormat("会話要約 %p%")
        self._minutes_progress.setVisible(False)
        self._minutes_stage.body_layout.addWidget(self._minutes_progress)
        flow.addWidget(self._minutes_stage, 6, 0, 1, 2)

        flow_note = QLabel(
            "話者分離と画面解析は任意です。先に完了してから会話要約を生成すると、"
            "利用可能な追加情報が要約へ反映されます。"
        )
        flow_note.setWordWrap(True)
        flow_note.setStyleSheet("color: #aeb4bd; padding: 4px 2px;")
        flow_note.setAccessibleName("解析工程についての説明")
        flow.addWidget(flow_note, 7, 0, 1, 2)
        analysis_layout.addLayout(flow)
        root.addWidget(analysis_group)
        root.addStretch(1)

        self._scroll.setWidget(content)
        shell.addWidget(self._scroll, 1)

        action_bar = QWidget()
        action = QHBoxLayout()
        action.setContentsMargins(0, 0, 0, 0)
        self._action_hint = QLabel("会議名と音声を選択すると録音を開始できます。")
        self._action_hint.setStyleSheet("color: #aeb4bd;")
        action.addWidget(self._action_hint)
        self._start = QPushButton("会議開始")
        self._start.setMinimumWidth(120)
        self._start.setMinimumHeight(34)
        self._start.setDefault(True)
        self._start.setEnabled(False)
        self._stop = QPushButton("会議終了")
        self._stop.setMinimumWidth(120)
        self._stop.setMinimumHeight(34)
        self._stop.setEnabled(False)
        action.addStretch(1)
        action.addWidget(self._start)
        action.addWidget(self._stop)
        action_bar.setLayout(action)
        shell.addWidget(action_bar)
        self.setCentralWidget(central)

        self._timer = QTimer(self)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._update_elapsed)
        self._source_refresh_timeout_timer = QTimer(self)
        self._source_refresh_timeout_timer.setSingleShot(True)
        self._source_refresh_timeout_timer.timeout.connect(self._on_source_refresh_timeout)
        self._refresh.clicked.connect(self.refresh_sources)
        self._preview_audio.clicked.connect(self._toggle_audio_preview)
        self._preview_screen.clicked.connect(self._toggle_screen_preview)
        self._start.clicked.connect(self._start_recording)
        self._stop.clicked.connect(self._stop_recording)
        self._reselect.clicked.connect(self._replace_screen)
        self._transcribe.clicked.connect(self._toggle_transcription)
        self._diarize.clicked.connect(self._toggle_diarization)
        self._save_speaker_names.clicked.connect(self._update_speaker_names)
        self._analyze_screens.clicked.connect(self._toggle_screen_analysis)
        self._generate_minutes.clicked.connect(self._toggle_minutes)
        self._open_recordings.clicked.connect(self._open_recordings_directory)
        self._open_session.clicked.connect(self._open_selected_session)
        self._open_transcript.clicked.connect(self._open_selected_transcript)
        self._open_screen_analysis.clicked.connect(self._open_selected_screen_analysis)
        self._open_minutes.clicked.connect(self._open_selected_minutes)
        self._refresh_sessions.clicked.connect(lambda: self.refresh_analysis_sessions())
        self._analysis_session.currentIndexChanged.connect(self._on_analysis_session_changed)
        self._auto_transcribe.toggled.connect(self._on_auto_transcription_toggled)
        self._title.textChanged.connect(self._on_title_changed)
        self._microphone.currentIndexChanged.connect(self._on_audio_source_changed)
        self._system_audio.currentIndexChanged.connect(self._on_audio_source_changed)
        self._screen_target.currentIndexChanged.connect(self._on_screen_target_changed)
        controller.component_changed.connect(self._on_component_changed)
        controller.meter_changed.connect(self._on_meter_changed)
        controller.screenshot_count_changed.connect(
            lambda count: self._screenshots.setText(f"保存画像 {count}")
        )
        controller.sources_refreshed.connect(self._on_sources_refreshed)
        controller.screen_preview_ready.connect(self._on_screen_preview_ready)
        controller.screen_preview_failed.connect(self._on_screen_preview_failed)
        controller.screen_preview_cancelled.connect(self._on_screen_preview_cancelled)
        controller.audio_preview_finished.connect(self._on_audio_preview_finished)
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

    @staticmethod
    def _flow_arrow(accessible_description: str) -> QLabel:
        arrow = QLabel("↓")
        arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
        arrow.setStyleSheet("color: #8fa8bf; font-size: 20px; font-weight: 700;")
        arrow.setAccessibleName(accessible_description)
        arrow.setToolTip(accessible_description)
        return arrow

    def _on_title_changed(self, _text: str) -> None:
        if self._title.text().strip():
            self._title_error.clear()
            self._title_error.setVisible(False)
        self._update_start_enabled()

    def _show_title_error(self, message: str) -> None:
        self._title_error.setText(message)
        self._title_error.setVisible(True)
        self._title.setFocus()

    def _any_analysis_running(self) -> bool:
        return any(
            controller is not None and controller.is_running
            for controller in (
                self._transcription_controller,
                self._diarization_controller,
                self._screen_analysis_controller,
                self._minutes_controller,
            )
        )

    def _update_start_enabled(self) -> None:
        has_title = bool(self._title.text().strip())
        has_audio = isinstance(self._microphone.currentData(), AudioDevice) or isinstance(
            self._system_audio.currentData(), AudioDevice
        )
        idle = (
            not self._controller.is_recording
            and not self._source_refresh_pending
            and not self._screen_preview_pending
            and not self._audio_preview_pending
            and not self._any_analysis_running()
            and self._title.isEnabled()
        )
        self._start.setEnabled(idle and has_title and has_audio)
        if self._controller.is_recording:
            self._action_hint.setText("録音中です。終了後に新しい会議を開始できます。")
        elif self._source_refresh_pending:
            self._action_hint.setText("録音デバイスを確認しています。")
        elif self._screen_preview_pending:
            self._action_hint.setText("画面プレビューの終了後に会議を開始できます。")
        elif self._audio_preview_pending:
            self._action_hint.setText("音声入力テストの終了後に会議を開始できます。")
        elif self._any_analysis_running():
            self._action_hint.setText("解析処理の完了後に録音を開始できます。")
        elif not has_title:
            self._action_hint.setText("会議名を入力してください。")
        elif not has_audio:
            self._action_hint.setText("マイクまたはPC音声を選択してください。")
        else:
            self._action_hint.setText("録音を開始できます。")

    def _open_recordings_directory(self) -> None:
        self._open_local_path(self._controller.meetings_directory, "録音保存先が見つかりません。")

    def _open_selected_session(self) -> None:
        summary = self._selected_analysis_session()
        if summary is None:
            self.show_error("開く会議記録を選択してください。")
            return
        self._open_local_path(summary.path, "会議記録のフォルダが見つかりません。")

    def _open_selected_transcript(self) -> None:
        self._open_selected_output(
            Path("output/transcript.md"), "文字起こしファイルが見つかりません。"
        )

    def _open_selected_screen_analysis(self) -> None:
        self._open_selected_output(
            Path("analysis/screens.json"), "画面解析結果が見つかりません。"
        )

    def _open_selected_minutes(self) -> None:
        self._open_selected_output(Path("output/minutes.md"), "会話要約ファイルが見つかりません。")

    def _open_selected_output(self, relative_path: Path, missing_message: str) -> None:
        summary = self._selected_analysis_session()
        if summary is None:
            self.show_error("開く会議記録を選択してください。")
            return
        self._open_local_path(summary.path / relative_path, missing_message)

    def _open_local_path(self, path: Path, missing_message: str) -> None:
        if not path.exists():
            self.show_error(missing_message)
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve()))):
            self.show_error(f"開けませんでした: {path}")

    def refresh_sources(self) -> None:
        if self._screen_preview_pending or self._audio_preview_pending:
            self.show_warning("プレビューの終了後に一覧を更新してください。")
            return
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
            self._update_start_enabled()
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
        self._screen_preview.show_message("画面を選択してプレビューしてください。")
        self._update_screen_preview_control()
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
            self._update_start_enabled()
        self._update_screen_preview_control()
        self._update_audio_preview_control()

    def _on_screen_target_changed(self, index: int) -> None:
        self._update_idle_source_names(index)
        if not self._screen_preview_pending:
            self._screen_preview.show_message("画面を選択してプレビューしてください。")
        self._update_screen_preview_control()

    def _update_screen_preview_control(self) -> None:
        if self._screen_preview_pending:
            self._preview_screen.setText(
                "プレビューを中止" if not self._screen_preview_cancelling else "中止しています"
            )
            self._preview_screen.setEnabled(not self._screen_preview_cancelling)
            return
        self._preview_screen.setText("選択画面をプレビュー")
        target_selected = isinstance(self._screen_target.currentData(), ScreenTarget)
        can_preview = (
            target_selected
            and not self._controller.is_recording
            and not self._source_refresh_pending
            and not self._audio_preview_pending
            and self._title.isEnabled()
        )
        self._preview_screen.setEnabled(can_preview)

    def _toggle_screen_preview(self) -> None:
        if self._audio_preview_pending:
            self.show_warning("音声入力テストの終了後に画面をプレビューしてください。")
            return
        if self._screen_preview_pending:
            self._cancel_screen_preview()
            return
        target = self._screen_target.currentData()
        if not isinstance(target, ScreenTarget):
            self.show_error("プレビューする画面を選択してください。")
            return
        self._screen_preview_request_id += 1
        request_id = self._screen_preview_request_id
        self._screen_preview_pending = True
        self._screen_preview_cancelling = False
        self._screen_target.setEnabled(False)
        self._refresh.setEnabled(False)
        self._screen_preview.show_message(
            "画面プレビューを取得しています。WaylandではOSの選択画面に応答してください。"
        )
        self._update_screen_preview_control()
        self._update_audio_preview_control()
        self._update_start_enabled()
        try:
            self._controller.preview_screen_target_async(request_id, target)
        except Exception as exc:
            self._finish_screen_preview()
            self._screen_preview.show_message("画面プレビューを表示できませんでした。")
            self.show_error(f"画面プレビューを開始できません: {exc}")

    def _on_audio_source_changed(self, index: int) -> None:
        self._update_idle_source_names(index)
        self._update_audio_preview_control()

    def _update_audio_preview_control(self) -> None:
        if self._audio_preview_pending:
            self._preview_audio.setText(
                "音声テストを停止" if not self._audio_preview_cancelling else "停止しています"
            )
            self._preview_audio.setEnabled(not self._audio_preview_cancelling)
            return
        self._preview_audio.setText("マイク・PC音源をテスト")
        source_selected = isinstance(
            self._microphone.currentData(), AudioDevice
        ) or isinstance(self._system_audio.currentData(), AudioDevice)
        can_preview = (
            source_selected
            and not self._controller.is_recording
            and not self._source_refresh_pending
            and not self._screen_preview_pending
            and self._title.isEnabled()
        )
        self._preview_audio.setEnabled(can_preview)

    def _toggle_audio_preview(self) -> None:
        if self._audio_preview_pending:
            self._cancel_audio_preview()
            return
        microphone = self._microphone.currentData()
        system_audio = self._system_audio.currentData()
        if not isinstance(microphone, AudioDevice):
            microphone = None
        if not isinstance(system_audio, AudioDevice):
            system_audio = None
        if microphone is None and system_audio is None:
            self.show_error("テストするマイクまたはPC音源を選択してください。")
            return
        self._audio_preview_request_id += 1
        request_id = self._audio_preview_request_id
        self._audio_preview_pending = True
        self._audio_preview_cancelling = False
        self._microphone.setEnabled(False)
        self._system_audio.setEnabled(False)
        self._screen_target.setEnabled(False)
        self._refresh.setEnabled(False)
        self._update_audio_preview_control()
        self._update_screen_preview_control()
        self._update_start_enabled()
        self.show_information(
            "音声入力をテストしています。マイクへ話しかけ、PC音源を再生してメーターを確認してください。"
        )
        try:
            self._controller.preview_audio_sources_async(
                request_id,
                microphone,
                system_audio,
            )
        except Exception as exc:
            self._finish_audio_preview()
            self.show_error(f"音声入力テストを開始できません: {exc}")

    def _cancel_audio_preview(self) -> None:
        self._audio_preview_cancelling = True
        self._update_audio_preview_control()
        self.show_information("音声入力テストを停止しています。")
        try:
            self._controller.cancel_audio_preview()
        except Exception as exc:
            self._finish_audio_preview()
            self.show_error(f"音声入力テストを停止できません: {exc}")

    def _on_audio_preview_finished(self, request_id: int, value: object) -> None:
        if request_id != self._audio_preview_request_id:
            return
        errors = tuple(str(item) for item in value) if isinstance(value, tuple | list) else ()
        self._finish_audio_preview(reset_states=not errors)
        if errors:
            self.show_error(" / ".join(errors))
        else:
            self.show_information("音声入力テストを終了しました。")

    def _finish_audio_preview(self, *, reset_states: bool = True) -> None:
        self._audio_preview_pending = False
        self._audio_preview_cancelling = False
        can_edit = not self._controller.is_recording and self._title.isEnabled()
        self._microphone.setEnabled(can_edit)
        self._system_audio.setEnabled(can_edit)
        self._screen_target.setEnabled(can_edit)
        self._refresh.setEnabled(can_edit)
        if reset_states:
            self._set_idle_audio_states()
        self._update_audio_preview_control()
        self._update_screen_preview_control()
        self._update_start_enabled()

    def _cancel_screen_preview(self) -> None:
        self._screen_preview_cancelling = True
        self._screen_preview.show_message("画面プレビューを中止しています。")
        self._update_screen_preview_control()
        try:
            self._controller.cancel_screen_preview()
        except Exception as exc:
            self._finish_screen_preview()
            self.show_error(f"画面プレビューを中止できません: {exc}")

    def _on_screen_preview_ready(self, request_id: int, value: object) -> None:
        if request_id != self._screen_preview_request_id:
            return
        self._finish_screen_preview()
        if not isinstance(value, np.ndarray):
            self._screen_preview.show_message("画面プレビューを表示できませんでした。")
            self.show_error("画面プレビューの画像形式を読み取れませんでした。")
            return
        try:
            self._screen_preview.show_frame(value)
        except ValueError as exc:
            self._screen_preview.show_message("画面プレビューを表示できませんでした。")
            self.show_error(str(exc))
            return
        target = self._screen_target.currentData()
        title = target.title if isinstance(target, ScreenTarget) else "選択画面"
        self.show_information(f"プレビューを表示しました: {title}")

    def _on_screen_preview_failed(self, request_id: int, message: str) -> None:
        if request_id != self._screen_preview_request_id:
            return
        self._finish_screen_preview()
        self._screen_preview.show_message("画面プレビューを表示できませんでした。")
        detail = message.strip() or "共有対象を確認して再試行してください。"
        self.show_error(f"画面プレビューを取得できません: {detail}")

    def _on_screen_preview_cancelled(self, request_id: int) -> None:
        if request_id != self._screen_preview_request_id:
            return
        self._finish_screen_preview()
        self._screen_preview.show_message("画面を選択してプレビューしてください。")
        self.show_information("画面プレビューを中止しました。")

    def _finish_screen_preview(self) -> None:
        self._screen_preview_pending = False
        self._screen_preview_cancelling = False
        can_edit = not self._controller.is_recording and self._title.isEnabled()
        self._screen_target.setEnabled(can_edit)
        self._refresh.setEnabled(can_edit)
        self._update_screen_preview_control()
        self._update_audio_preview_control()
        self._update_start_enabled()

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
        title = self._title.text().strip()
        if not title:
            self._show_title_error("会議名を入力してください。")
            self._update_start_enabled()
            return
        if not (
            isinstance(self._microphone.currentData(), AudioDevice)
            or isinstance(self._system_audio.currentData(), AudioDevice)
        ):
            self.show_error("マイクまたはPC音声を選択してください。")
            self._update_start_enabled()
            return
        try:
            self._controller.start_session(
                title=title,
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
        self._transcribe.setEnabled(False)
        self._diarize.setEnabled(False)
        self._analyze_screens.setEnabled(False)
        self._generate_minutes.setEnabled(False)
        self._save_stage.set_state(
            AnalysisStageState.WAITING,
            detail="録音デバイスを準備しています。会議終了後に音声を保存します。",
            status_text="準備中",
        )
        for stage in (
            self._transcription_stage,
            self._diarization_stage,
            self._screen_analysis_stage,
            self._minutes_stage,
        ):
            stage.set_state(
                AnalysisStageState.WAITING,
                detail="録音の保存完了後に状態を確認します。",
            )
        self._clear_speaker_names()
        self._action_hint.setText("録音デバイスを準備しています。")
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
        self._save_stage.set_state(
            AnalysisStageState.WAITING,
            detail="会議終了後に録音ファイルを保存します。",
            status_text="会議中",
        )
        self._action_hint.setText("録音中です。終了後に新しい会議を開始できます。")
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
        if error_message:
            self._save_stage.set_state(
                AnalysisStageState.FAILED,
                detail="録音の保存中に問題が発生しました。エラー内容を確認してください。",
                status_text="保存失敗",
            )
        self._maybe_start_auto_transcription(Path(path), error_message=error_message)
        self._close_if_requested()

    def _on_finalize_progress(self, percent: int, message: str) -> None:
        self._set_inputs_enabled(False)
        self._start.setEnabled(False)
        self._stop.setEnabled(False)
        self._reselect.setEnabled(False)
        self._finalize_progress.setValue(percent)
        self._finalize_progress.setFormat(f"保存処理 %p% - {message}")
        self._finalize_progress.setVisible(True)
        self._save_stage.set_state(
            AnalysisStageState.RUNNING,
            detail=message,
            status_text="保存中",
        )
        self.show_information(message)

    def _on_session_start_failed(self, path: str, message: str) -> None:
        self._session_path = Path(path)
        self._show_save_path(self._session_path)
        self._reset_after_session()
        self._save_stage.set_state(
            AnalysisStageState.FAILED,
            detail=message,
            status_text="開始失敗",
        )
        self.show_error(message)
        self._close_if_requested()

    def _on_session_start_cancelled(self, path: str) -> None:
        self._session_path = Path(path)
        self._show_save_path(self._session_path)
        self._reset_after_session()
        self._save_stage.set_state(
            AnalysisStageState.CANCELED,
            detail="録音の開始をキャンセルしました。",
        )
        self.show_information("録音の開始をキャンセルしました。")
        self._close_if_requested()

    def _reset_after_session(self) -> None:
        self._timer.stop()
        self._started_at = None
        self._set_inputs_enabled(True)
        self._update_start_enabled()
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
            self._transcription_stage.set_state(
                AnalysisStageState.RUNNING,
                detail="文字起こしの停止を待っています。",
                status_text="キャンセル中",
            )
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

    def _toggle_diarization(self) -> None:
        controller = self._diarization_controller
        if controller is None:
            self.show_error("話者分離機能を初期化できませんでした。")
            return
        if controller.is_running:
            self._diarize.setEnabled(False)
            self._diarization_stage.set_state(
                AnalysisStageState.RUNNING,
                detail="話者分離の停止を待っています。",
                status_text="キャンセル中",
            )
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
            self._screen_analysis_stage.set_state(
                AnalysisStageState.RUNNING,
                detail="画面解析の停止を待っています。",
                status_text="キャンセル中",
            )
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
            self.show_error("会話要約機能を初期化できませんでした。")
            return
        if controller.is_running:
            self._generate_minutes.setEnabled(False)
            self._minutes_stage.set_state(
                AnalysisStageState.RUNNING,
                detail="会話要約の停止を待っています。",
                status_text="キャンセル中",
            )
            controller.cancel()
            return
        summary = self._selected_analysis_session()
        if summary is None:
            self.show_error("要約する会話記録がありません。")
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
        self.show_information(f"話者名と会話要約を更新しました: {output}")

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

    def _on_auto_transcription_toggled(self, enabled: bool) -> None:
        try:
            self._controller.set_auto_transcribe_after_recording(enabled)
        except Exception as exc:
            self._auto_transcribe.blockSignals(True)
            self._auto_transcribe.setChecked(self._controller.auto_transcribe_after_recording)
            self._auto_transcribe.blockSignals(False)
            self.show_error(str(exc))

    def _on_transcription_started(self, session_path: str) -> None:
        if not self._is_current_session(session_path):
            return
        self._set_inputs_enabled(False)
        self._start.setEnabled(False)
        self._transcription_stage.set_state(
            AnalysisStageState.RUNNING,
            detail="文字起こしモデルを準備しています。",
        )
        self._transcribe.setText("文字起こしをキャンセル")
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
        self._transcription_stage.set_state(AnalysisStageState.RUNNING, detail=message)
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
        self._diarization_stage.set_state(
            AnalysisStageState.RUNNING,
            detail="PC音声から話者を分離しています。",
        )
        self._diarize.setText("話者分離をキャンセル")
        self._diarize.setEnabled(True)
        self._diarization_progress.setValue(0)
        self._diarization_progress.setVisible(True)
        self._speaker_names_widget.setEnabled(False)
        self._save_speaker_names.setVisible(False)
        self.show_information("PC音声から話者を分離しています。")

    def _on_diarization_progress(self, percent: int, message: str) -> None:
        self._diarization_progress.setValue(percent)
        self._diarization_progress.setFormat(f"話者分離 %p% - {message}")
        self._diarization_stage.set_state(AnalysisStageState.RUNNING, detail=message)
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
        self._screen_analysis_stage.set_state(
            AnalysisStageState.RUNNING,
            detail="保存済みスクリーンショットを解析しています。",
        )
        self._analyze_screens.setText("画面解析をキャンセル")
        self._analyze_screens.setEnabled(True)
        self._screen_analysis_progress.setValue(0)
        self._screen_analysis_progress.setVisible(True)
        self.show_information("保存済みスクリーンショットを解析しています。")

    def _on_screen_analysis_progress(self, percent: int, message: str) -> None:
        self._screen_analysis_progress.setValue(percent)
        self._screen_analysis_progress.setFormat(f"画面解析 %p% - {message}")
        self._screen_analysis_stage.set_state(AnalysisStageState.RUNNING, detail=message)
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
        self._minutes_stage.set_state(
            AnalysisStageState.RUNNING,
            detail="ローカルLLMで会話内容を要約しています。",
        )
        self._generate_minutes.setText("要約生成をキャンセル")
        self._generate_minutes.setEnabled(True)
        self._minutes_progress.setValue(0)
        self._minutes_progress.setVisible(True)
        self.show_information("ローカルLLMで会話内容を要約しています。")

    def _on_minutes_progress(self, percent: int, message: str) -> None:
        self._minutes_progress.setValue(percent)
        self._minutes_progress.setFormat(f"会話要約 %p% - {message}")
        self._minutes_stage.set_state(AnalysisStageState.RUNNING, detail=message)
        self.show_information(message)

    def _on_minutes_finished(self, session_path: str, output_path: str) -> None:
        if not self._is_current_session(session_path):
            return
        self._finish_minutes_ui("完了")
        self.refresh_analysis_sessions(Path(session_path))
        self.show_information(f"会話要約を保存しました: {output_path}")

    def _on_minutes_failed(self, session_path: str, message: str) -> None:
        if not self._is_current_session(session_path):
            return
        self._finish_minutes_ui("失敗")
        self.show_error(message)

    def _on_minutes_canceled(self, session_path: str) -> None:
        if not self._is_current_session(session_path):
            return
        self._finish_minutes_ui("キャンセル")
        self.show_information("会話要約をキャンセルしました。")

    def _finish_minutes_ui(self, status: str) -> None:
        self._set_finished_stage(
            self._minutes_stage,
            status,
            completed_detail="会話要約を保存しました。",
        )
        self._generate_minutes.setText("要約を再生成")
        self._minutes_progress.setVisible(False)
        self._set_inputs_enabled(True)
        self._update_analysis_availability()
        self._update_start_enabled()

    def _finish_screen_analysis_ui(self, status: str) -> None:
        self._set_finished_stage(
            self._screen_analysis_stage,
            status,
            completed_detail="画面解析結果を保存しました。",
        )
        self._analyze_screens.setText("画面解析を再実行")
        self._screen_analysis_progress.setVisible(False)
        self._set_inputs_enabled(True)
        self._update_analysis_availability()
        self._update_start_enabled()

    def _finish_diarization_ui(self, status: str) -> None:
        self._set_finished_stage(
            self._diarization_stage,
            status,
            completed_detail="話者付き文字起こしを保存しました。",
        )
        self._diarize.setText("話者分離を再実行")
        self._diarization_progress.setVisible(False)
        self._speaker_names_widget.setEnabled(True)
        self._set_inputs_enabled(True)
        self._update_analysis_availability()
        self._update_start_enabled()

    def _finish_transcription_ui(self, status: str) -> None:
        self._set_finished_stage(
            self._transcription_stage,
            status,
            completed_detail="文字起こし結果を保存しました。",
        )
        self._transcribe.setText("文字起こしを再実行")
        self._transcribe.setEnabled(True)
        self._transcription_progress.setVisible(False)
        self._set_inputs_enabled(True)
        self._update_analysis_availability()
        self._update_start_enabled()

    def _update_analysis_availability(self) -> None:
        summary = self._selected_analysis_session()
        controllers = (
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
            and (
                minutes.is_running
                or (minutes.is_configured and summary.can_generate_minutes)
            )
            and not other_running(minutes)
        )
        self._open_session.setEnabled(summary is not None and summary.path.exists())
        self._open_transcript.setEnabled(
            summary is not None and (summary.path / "output" / "transcript.md").is_file()
        )
        self._open_screen_analysis.setEnabled(
            summary is not None and (summary.path / "analysis" / "screens.json").is_file()
        )
        self._open_minutes.setEnabled(
            summary is not None and (summary.path / "output" / "minutes.md").is_file()
        )

    @staticmethod
    def _set_finished_stage(
        stage: AnalysisStageCard,
        status: str,
        *,
        completed_detail: str,
    ) -> None:
        state = {
            "完了": AnalysisStageState.COMPLETED,
            "失敗": AnalysisStageState.FAILED,
            "キャンセル": AnalysisStageState.CANCELED,
        }.get(status, AnalysisStageState.FAILED)
        detail = {
            "完了": completed_detail,
            "失敗": "処理に失敗しました。内容を確認して再実行できます。",
            "キャンセル": "処理をキャンセルしました。必要な場合は再実行できます。",
        }.get(status, "処理状態を確認できません。再実行してください。")
        stage.set_state(state, detail=detail, status_text=status)

    @staticmethod
    def _set_persisted_job_stage(
        stage: AnalysisStageCard,
        *,
        status: str,
        can_start: bool,
        ready_detail: str,
        blocked_state: AnalysisStageState,
        blocked_detail: str,
        completed_detail: str,
    ) -> None:
        if status == "SUCCEEDED":
            stage.set_state(AnalysisStageState.COMPLETED, detail=completed_detail)
            return
        if status == "NOT_STARTED":
            if can_start:
                stage.set_state(AnalysisStageState.READY, detail=ready_detail)
            else:
                stage.set_state(blocked_state, detail=blocked_detail)
            return
        state, text, detail = {
            "INCOMPLETE": (
                AnalysisStageState.FAILED,
                "要再実行",
                "出力が揃っていません。処理を再実行してください。",
            ),
            "UNKNOWN": (
                AnalysisStageState.FAILED,
                "状態不明",
                "保存された状態を確認できません。処理を再実行してください。",
            ),
            "FAILED": (
                AnalysisStageState.FAILED,
                "失敗",
                "前回の処理に失敗しました。再実行できます。",
            ),
            "CANCELED": (
                AnalysisStageState.CANCELED,
                "キャンセル",
                "前回の処理はキャンセルされました。再実行できます。",
            ),
            "RUNNING": (
                AnalysisStageState.FAILED,
                "前回中断",
                "前回の実行が中断されています。再実行してください。",
            ),
        }.get(
            status,
            (
                AnalysisStageState.FAILED,
                status,
                "保存された処理状態を確認してください。",
            ),
        )
        stage.set_state(state, detail=detail, status_text=text)

    def _sync_analysis_flow(self, summary: SessionSummary | None) -> None:
        if summary is None:
            for stage in (
                self._save_stage,
                self._transcription_stage,
                self._diarization_stage,
                self._screen_analysis_stage,
                self._minutes_stage,
            ):
                stage.set_state(
                    AnalysisStageState.UNAVAILABLE,
                    detail="解析対象の会議を選択してください。",
                )
            return

        if summary.recording_status == "RECORDED":
            self._save_stage.set_state(
                AnalysisStageState.COMPLETED,
                detail="録音ファイルの保存が完了しています。",
            )
        elif summary.recording_status in {"PREPARING", "RECORDING"}:
            self._save_stage.set_state(
                AnalysisStageState.WAITING,
                detail="会議終了後に録音ファイルを保存します。",
                status_text="会議中",
            )
        else:
            self._save_stage.set_state(
                AnalysisStageState.FAILED,
                detail="録音を正常に保存できていません。セッション情報を確認してください。",
                status_text="保存失敗",
            )

        transcription_available = self._transcription_controller is not None
        self._set_persisted_job_stage(
            self._transcription_stage,
            status=summary.transcription_status,
            can_start=summary.can_transcribe and transcription_available,
            ready_detail="保存された音声を文字に変換できます。",
            blocked_state=AnalysisStageState.UNAVAILABLE,
            blocked_detail=(
                "文字起こし機能を初期化できませんでした。"
                if not transcription_available
                else "文字起こし可能な音声がありません。"
            ),
            completed_detail="文字起こし結果を保存済みです。",
        )

        diarization_available = self._diarization_controller is not None
        transcription_completed = summary.transcription_status == "SUCCEEDED"
        if not transcription_completed:
            diarization_blocked_state = AnalysisStageState.WAITING
            diarization_blocked_detail = "文字起こしの完了後に実行できます。"
        elif not diarization_available:
            diarization_blocked_state = AnalysisStageState.UNAVAILABLE
            diarization_blocked_detail = "話者分離機能を初期化できませんでした。"
        else:
            diarization_blocked_state = AnalysisStageState.UNAVAILABLE
            diarization_blocked_detail = "PC音声がないため話者分離の対象外です。"
        self._set_persisted_job_stage(
            self._diarization_stage,
            status=summary.diarization_status,
            can_start=summary.can_diarize and diarization_available,
            ready_detail="PC音声から話者を分離できます。",
            blocked_state=diarization_blocked_state,
            blocked_detail=diarization_blocked_detail,
            completed_detail="話者付き文字起こしを保存済みです。",
        )

        screen_analysis_available = self._screen_analysis_controller is not None
        self._set_persisted_job_stage(
            self._screen_analysis_stage,
            status=summary.screen_analysis_status,
            can_start=summary.can_analyze_screens and screen_analysis_available,
            ready_detail="保存された画面画像を解析できます。",
            blocked_state=AnalysisStageState.UNAVAILABLE,
            blocked_detail=(
                "画面解析機能を初期化できませんでした。"
                if not screen_analysis_available
                else "保存画像がないため画面解析の対象外です。"
            ),
            completed_detail="画面解析結果を保存済みです。",
        )

        minutes = self._minutes_controller
        minutes_available = minutes is not None
        minutes_configured = minutes is not None and minutes.is_configured
        if summary.minutes_status != "SUCCEEDED" and not minutes_available:
            self._minutes_stage.set_state(
                AnalysisStageState.UNAVAILABLE,
                detail="会話要約機能を初期化できませんでした。",
            )
            return
        if summary.minutes_status != "SUCCEEDED" and not minutes_configured:
            self._minutes_stage.set_state(
                AnalysisStageState.UNAVAILABLE,
                detail=(
                    "data/settings.jsonのllm.base_urlを設定し、"
                    "アプリを再起動してください。"
                ),
            )
            return
        optional_pending: list[str] = []
        if summary.can_diarize and summary.diarization_status != "SUCCEEDED":
            optional_pending.append("話者分離")
        if summary.can_analyze_screens and summary.screen_analysis_status != "SUCCEEDED":
            optional_pending.append("画面解析")
        ready_detail = "会話要約を生成できます。"
        if optional_pending:
            ready_detail = (
                f"{'・'.join(optional_pending)}を先に完了すると、追加情報を要約へ反映できます。"
            )
        if not transcription_completed:
            minutes_blocked_state = AnalysisStageState.WAITING
            minutes_blocked_detail = "文字起こしの完了後に実行できます。"
        else:
            minutes_blocked_state = AnalysisStageState.UNAVAILABLE
            minutes_blocked_detail = "会話要約に必要な文字起こし結果がありません。"
        self._set_persisted_job_stage(
            self._minutes_stage,
            status=summary.minutes_status,
            can_start=(
                summary.can_generate_minutes and minutes_available and minutes_configured
            ),
            ready_detail=ready_detail,
            blocked_state=minutes_blocked_state,
            blocked_detail=minutes_blocked_detail,
            completed_detail="会話要約を保存済みです。",
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
        self._sync_analysis_flow(summary)
        if summary is None:
            self._transcribe.setText("文字起こしを実行")
            self._transcribe.setEnabled(False)
            self._diarize.setText("話者分離を実行")
            self._diarize.setEnabled(False)
            self._analyze_screens.setText("画面解析を実行")
            self._analyze_screens.setEnabled(False)
            self._generate_minutes.setText("要約を生成")
            self._generate_minutes.setEnabled(False)
            self._open_session.setEnabled(False)
            self._open_transcript.setEnabled(False)
            self._open_screen_analysis.setEnabled(False)
            self._open_minutes.setEnabled(False)
            self._clear_speaker_names()
            return
        self._transcribe.setText(
            "文字起こしを再実行"
            if summary.transcription_status != "NOT_STARTED"
            else "文字起こしを実行"
        )
        self._diarize.setText(
            "話者分離を再実行"
            if summary.diarization_status != "NOT_STARTED"
            else "話者分離を実行"
        )
        self._analyze_screens.setText(
            "画面解析を再実行"
            if summary.screen_analysis_status != "NOT_STARTED"
            else "画面解析を実行"
        )
        self._generate_minutes.setText(
            "要約を再生成"
            if summary.minutes_status != "NOT_STARTED"
            else "要約を生成"
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
        valid_names = [
            (speaker_id, name)
            for speaker_id, name in names.items()
            if isinstance(speaker_id, str) and isinstance(name, str)
        ]
        for index, (speaker_id, name) in enumerate(valid_names, start=1):
            editor = QLineEdit(name)
            editor.setAccessibleName(f"話者 {index} の名前")
            self._speaker_name_inputs[speaker_id] = editor
            label = QLabel(f"話者 {index}")
            label.setToolTip(f"内部ID: {speaker_id}")
            self._speaker_names_layout.addRow(label, editor)
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
        self._microphone.setEnabled(enabled and not self._audio_preview_pending)
        self._system_audio.setEnabled(enabled and not self._audio_preview_pending)
        preview_pending = self._screen_preview_pending or self._audio_preview_pending
        self._refresh.setEnabled(enabled and not preview_pending)
        self._screen_target.setEnabled(enabled and not preview_pending)
        self._analysis_session.setEnabled(enabled)
        self._refresh_sessions.setEnabled(enabled)
        self._auto_transcribe.setEnabled(enabled)
        self._speaker_count.setEnabled(enabled)
        self._speaker_names_widget.setEnabled(enabled)
        self._save_speaker_names.setEnabled(enabled)
        self._update_screen_preview_control()
        self._update_audio_preview_control()

    def show_information(self, message: str) -> None:
        self._message.setText(message)
        self._message.setStyleSheet(
            "padding: 10px; background: #243447; color: #e6f2ff; "
            "border: 1px solid #3f5871; border-radius: 4px;"
        )

    def show_error(self, message: str) -> None:
        self._message.setText(message)
        self._message.setStyleSheet(
            "padding: 10px; background: #4a2024; color: #ffdad6; "
            "border: 1px solid #8c3d45; border-radius: 4px;"
        )

    def show_warning(self, message: str) -> None:
        self._message.setText(message)
        self._message.setStyleSheet(
            "padding: 10px; background: #463b18; color: #ffe082; "
            "border: 1px solid #806d2c; border-radius: 4px;"
        )

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
        if not self._audio_preview_pending:
            self._set_idle_audio_states()
        self._update_audio_preview_control()
        self._update_start_enabled()

    def _set_idle_audio_states(self) -> None:
        microphone = self._microphone.currentData()
        system_audio = self._system_audio.currentData()
        self._mic_status.set_state(
            "READY" if isinstance(microphone, AudioDevice) else "NOT_CONFIGURED"
        )
        self._system_status.set_state(
            "READY" if isinstance(system_audio, AudioDevice) else "NOT_CONFIGURED"
        )

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
        if self._audio_preview_pending:
            self._controller.cancel_audio_preview()
        if self._screen_preview_pending:
            self._controller.cancel_screen_preview()
        self._set_inputs_enabled(False)
        self._start.setEnabled(False)
        self._stop.setEnabled(False)
        self._reselect.setEnabled(False)
        if self._controller.is_recording:
            self.show_information("OSの終了に備えて記録を保存しています。")
        if self._transcription_controller is not None:
            self._transcription_controller.cancel()
        if self._diarization_controller is not None:
            self._diarization_controller.cancel()
        if self._screen_analysis_controller is not None:
            self._screen_analysis_controller.cancel()
        if self._minutes_controller is not None:
            self._minutes_controller.cancel()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self._audio_preview_pending:
            self._controller.cancel_audio_preview()
        if self._screen_preview_pending:
            self._controller.cancel_screen_preview()
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
        if self._diarization_controller is not None:
            self._diarization_controller.cancel()
        if self._screen_analysis_controller is not None:
            self._screen_analysis_controller.cancel()
        if self._minutes_controller is not None:
            self._minutes_controller.cancel()
        event.accept()
