"""会話要約workerの前提条件とQt通知を管理する。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from summarize_meeting.application.analysis_job_runner import (
    AnalysisJobCallbacks,
    AnalysisJobRunner,
)
from summarize_meeting.domain.analysis_job import AnalysisJobState
from summarize_meeting.infrastructure.analysis_job_repository import FileAnalysisJobRepository
from summarize_meeting.infrastructure.paths import PortableAppPaths


class MinutesController(QObject):
    """LLM設定を検証し、会話要約workerを起動する。"""

    job_started = Signal(str)
    job_progress = Signal(int, str)
    job_finished = Signal(str, str)
    job_failed = Signal(str, str)
    job_canceled = Signal(str)

    def __init__(
        self,
        app_paths: PortableAppPaths,
        *,
        base_url: str | None = None,
        model: str | None = None,
        job_repository: FileAnalysisJobRepository | None = None,
    ) -> None:
        super().__init__()
        self._app_paths = app_paths
        configured_url = os.environ.get("SUMMARIZE_MEETING_LLM_URL") or base_url
        normalized_url = configured_url.strip().rstrip("/") if configured_url else None
        self._base_url = normalized_url or None
        configured_model = model or os.environ.get("SUMMARIZE_MEETING_LLM_MODEL")
        self._model = configured_model.strip() if configured_model else None
        self._runner = AnalysisJobRunner(
            job_repository or FileAnalysisJobRepository(),
            display_name="会話要約",
            thread_name="minutes-job",
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

    @property
    def is_configured(self) -> bool:
        return self._base_url is not None

    def start(self, session_directory: Path) -> None:
        if self._base_url is None:
            raise RuntimeError(
                "LLMエンドポイントが未設定です。data/settings.jsonのllm.base_urlを設定し、"
                "アプリを再起動してください"
            )
        session_directory = session_directory.resolve()
        try:
            session_directory.relative_to(self._app_paths.meetings_dir.resolve())
        except ValueError as exc:
            raise ValueError("アプリのmeetingsフォルダ外は会話要約できません") from exc
        if not (session_directory / "analysis" / "transcription.json").is_file():
            raise RuntimeError("文字起こし結果がありません")
        state = AnalysisJobState.start(
            job="minutes",
            model=self._model or "llama.cpp auto",
            language="ja",
        )
        command = [
            sys.executable,
            "-m",
            "summarize_meeting.processing.minutes_worker",
            "--session",
            str(session_directory),
            "--base-url",
            self._base_url,
        ]
        if self._model is not None:
            command.extend(["--model", self._model])
        self._runner.start(session_directory, state, command)

    def cancel(self) -> None:
        self._runner.cancel()

    def wait(self, timeout_seconds: float | None = None) -> bool:
        return self._runner.wait(timeout_seconds)

    def shutdown(self, timeout_seconds: float | None = None) -> bool:
        return self._runner.shutdown(timeout_seconds)
