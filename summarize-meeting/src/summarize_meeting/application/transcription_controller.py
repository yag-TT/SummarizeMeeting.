"""文字起こしを別プロセスで実行し、JSON行の進捗をQt Signalへ変換する。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from summarize_meeting.application.worker_process import (
    platform_popen_options,
    terminate_process_tree,
)
from summarize_meeting.domain.analysis_job import AnalysisJobState, AnalysisJobStatus
from summarize_meeting.infrastructure.analysis_job_repository import (
    FileAnalysisJobRepository,
)
from summarize_meeting.infrastructure.paths import PortableAppPaths


class TranscriptionController(QObject):
    """文字起こしworkerの起動、キャンセル、永続ジョブ状態を管理する。"""

    job_started = Signal(str)
    job_progress = Signal(int, str)
    job_finished = Signal(str, str)
    job_failed = Signal(str, str)
    job_canceled = Signal(str)

    def __init__(
        self,
        app_paths: PortableAppPaths,
        *,
        model_name: str = "large-v3-turbo",
        language: str = "ja",
        job_repository: FileAnalysisJobRepository | None = None,
    ) -> None:
        super().__init__()
        self._app_paths = app_paths
        self._model_name = model_name
        self._language = language
        self._job_repository = job_repository or FileAnalysisJobRepository()
        self._lock = threading.RLock()
        self._process: subprocess.Popen[str] | None = None
        self._session_directory: Path | None = None
        self._cancel_requested = False
        self._running = False

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def start(self, session_directory: Path) -> None:
        session_directory = session_directory.resolve()
        state = AnalysisJobState.start(
            job="transcription",
            model=self._model_name,
            language=self._language,
        )
        with self._lock:
            if self._running:
                raise RuntimeError("文字起こしは既に実行中です")
            self._session_directory = session_directory
            self._cancel_requested = False
            self._running = True
        try:
            self._job_repository.save(session_directory, state)
        except OSError as exc:
            self._clear_process()
            raise RuntimeError(f"文字起こし状態を保存できません: {exc}") from exc
        thread = threading.Thread(
            target=self._run_worker,
            args=(session_directory, state),
            name="transcription-job",
            daemon=True,
        )
        try:
            thread.start()
        except RuntimeError as exc:
            self._persist_terminal(
                session_directory,
                state,
                AnalysisJobStatus.FAILED,
                error_message=str(exc),
            )
            self._clear_process()
            raise

    def cancel(self) -> None:
        with self._lock:
            self._cancel_requested = True
            process = self._process
        if process is not None and process.poll() is None:
            terminate_process_tree(process)

    def _run_worker(self, session_directory: Path, state: AnalysisJobState) -> None:
        self.job_started.emit(str(session_directory))
        command = [
            sys.executable,
            "-m",
            "summarize_meeting.processing.transcription_worker",
            "--session",
            str(session_directory),
            "--models-dir",
            str(self._app_paths.models_dir),
            "--model",
            self._model_name,
            "--language",
            self._language,
        ]
        environment = os.environ.copy()
        environment["PYTHONUTF8"] = "1"
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                **platform_popen_options(),
            )
        except OSError as exc:
            message = f"文字起こしを開始できません: {exc}"
            persistence_error = self._persist_terminal(
                session_directory,
                state,
                AnalysisJobStatus.FAILED,
                error_message=message,
            )
            self._clear_process()
            self.job_failed.emit(
                str(session_directory),
                _join_errors(message, persistence_error),
            )
            return
        with self._lock:
            self._process = process
            cancel_requested = self._cancel_requested
        if cancel_requested:
            terminate_process_tree(process)

        output_path: str | None = None
        diagnostic_lines: deque[str] = deque(maxlen=20)
        assert process.stdout is not None
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                diagnostic_lines.append(line)
                continue
            if not isinstance(value, dict):
                continue
            if value.get("type") == "progress":
                percent = value.get("percent")
                message = value.get("message")
                if isinstance(percent, int) and isinstance(message, str):
                    self.job_progress.emit(percent, message)
            elif value.get("type") == "result" and isinstance(value.get("path"), str):
                output_path = value["path"]
        exit_code = process.wait()
        with self._lock:
            canceled = self._cancel_requested
        self._clear_process()
        if canceled:
            persistence_error = self._persist_terminal(
                session_directory,
                state,
                AnalysisJobStatus.CANCELED,
            )
            if persistence_error is None:
                self.job_canceled.emit(str(session_directory))
            else:
                self.job_failed.emit(str(session_directory), persistence_error)
        elif exit_code == 0 and output_path is not None:
            persistence_error = self._persist_terminal(
                session_directory,
                state,
                AnalysisJobStatus.SUCCEEDED,
                output_path=_relative_output_path(session_directory, output_path),
            )
            if persistence_error is None:
                self.job_finished.emit(str(session_directory), output_path)
            else:
                self.job_failed.emit(str(session_directory), persistence_error)
        else:
            detail = diagnostic_lines[-1] if diagnostic_lines else f"終了コード {exit_code}"
            message = f"文字起こしに失敗しました: {detail}"
            persistence_error = self._persist_terminal(
                session_directory,
                state,
                AnalysisJobStatus.FAILED,
                error_message=message,
            )
            self.job_failed.emit(
                str(session_directory),
                _join_errors(message, persistence_error),
            )

    def _persist_terminal(
        self,
        session_directory: Path,
        state: AnalysisJobState,
        status: AnalysisJobStatus,
        *,
        output_path: str | None = None,
        error_message: str | None = None,
    ) -> str | None:
        try:
            self._job_repository.save(
                session_directory,
                state.finish(
                    status,
                    output_path=output_path,
                    error_message=error_message,
                ),
            )
        except OSError as exc:
            return f"文字起こし状態を保存できません: {exc}"
        return None

    def _clear_process(self) -> None:
        with self._lock:
            self._process = None
            self._session_directory = None
            self._cancel_requested = False
            self._running = False


def _relative_output_path(session_directory: Path, value: str) -> str:
    path = Path(value)
    try:
        return path.resolve().relative_to(session_directory).as_posix()
    except ValueError:
        return value


def _join_errors(message: str, persistence_error: str | None) -> str:
    return message if persistence_error is None else f"{message} / {persistence_error}"
