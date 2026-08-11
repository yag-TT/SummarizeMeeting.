"""画面解析workerの前提条件とQt通知を管理する。"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from summarize_meeting.application.analysis_job_runner import (
    AnalysisJobCallbacks,
    AnalysisJobRunner,
)
from summarize_meeting.domain.analysis_job import AnalysisJobState
from summarize_meeting.infrastructure.analysis_job_repository import (
    FileAnalysisJobRepository,
)
from summarize_meeting.infrastructure.paths import PortableAppPaths
from summarize_meeting.processing.screen_analysis import default_screen_analysis_runtime


class ScreenAnalysisController(QObject):
    """保存済み画面イベントを検証し、OCR workerを起動する。"""

    job_started = Signal(str)
    job_progress = Signal(int, str)
    job_finished = Signal(str, str)
    job_failed = Signal(str, str)
    job_canceled = Signal(str)

    def __init__(
        self,
        app_paths: PortableAppPaths,
        *,
        language: str = "ja",
        job_repository: FileAnalysisJobRepository | None = None,
    ) -> None:
        super().__init__()
        self._app_paths = app_paths
        self._language = language
        self._runner = AnalysisJobRunner(
            job_repository or FileAnalysisJobRepository(),
            display_name="画面解析",
            thread_name="screen-analysis-job",
            callbacks=AnalysisJobCallbacks(
                started=self.job_started.emit,
                progress=self.job_progress.emit,
                finished=self.job_finished.emit,
                failed=self.job_failed.emit,
                canceled=self.job_canceled.emit,
            ),
        )

    @property
    def is_running(self) -> bool:
        return self._runner.is_running

    def start(self, session_directory: Path) -> None:
        session_directory = session_directory.resolve()
        try:
            session_directory.relative_to(self._app_paths.meetings_dir.resolve())
        except ValueError as exc:
            raise ValueError("アプリのmeetingsフォルダ外は解析できません") from exc
        if not (session_directory / "screenshots" / "events.jsonl").is_file():
            raise RuntimeError("画面イベントがありません")
        state = AnalysisJobState.start(
            job="screen_analysis",
            model=default_screen_analysis_runtime(),
            language=self._language,
        )
        self._runner.start(
            session_directory,
            state,
            [
                sys.executable,
                "-m",
                "summarize_meeting.processing.screen_analysis_worker",
                "--session",
                str(session_directory),
                "--models-dir",
                str(self._app_paths.models_dir),
                "--language",
                self._language,
            ],
        )

    def cancel(self) -> None:
        self._runner.cancel()

    def wait(self, timeout_seconds: float | None = None) -> bool:
        return self._runner.wait(timeout_seconds)

    def shutdown(self, timeout_seconds: float | None = None) -> bool:
        return self._runner.shutdown(timeout_seconds)
