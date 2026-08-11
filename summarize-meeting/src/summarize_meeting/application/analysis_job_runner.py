"""録音後解析workerに共通するプロセスライフサイクルを管理する。"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from summarize_meeting.application.worker_process import (
    platform_popen_options,
    terminate_process_tree,
)
from summarize_meeting.domain.analysis_job import AnalysisJobState, AnalysisJobStatus
from summarize_meeting.infrastructure.analysis_job_repository import (
    FileAnalysisJobRepository,
)

EnvironmentFactory = Callable[[], Mapping[str, str]]


@dataclass(frozen=True)
class AnalysisJobCallbacks:
    started: Callable[[str], None]
    progress: Callable[[int, str], None]
    finished: Callable[[str, str], None]
    failed: Callable[[str, str], None]
    canceled: Callable[[str], None]


class AnalysisJobRunner:
    """1つのworkerを起動し、状態保存と終了通知を一貫して行う。"""

    def __init__(
        self,
        repository: FileAnalysisJobRepository,
        *,
        display_name: str,
        thread_name: str,
        callbacks: AnalysisJobCallbacks,
    ) -> None:
        self._repository = repository
        self._display_name = display_name
        self._thread_name = thread_name
        self._callbacks = callbacks
        self._lock = threading.RLock()
        self._process: subprocess.Popen[str] | None = None
        self._thread: threading.Thread | None = None
        self._cancel_requested = False
        self._running = False

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def start(
        self,
        session_directory: Path,
        state: AnalysisJobState,
        command: Sequence[str],
        *,
        environment_factory: EnvironmentFactory | None = None,
    ) -> None:
        with self._lock:
            if self._running:
                raise RuntimeError(f"{self._display_name}は既に実行中です")
            self._cancel_requested = False
            self._running = True
        try:
            self._repository.save(session_directory, state)
        except OSError as exc:
            self._clear()
            raise RuntimeError(f"{self._display_name}状態を保存できません: {exc}") from exc

        thread = threading.Thread(
            target=self._run,
            args=(session_directory, state, list(command), environment_factory),
            name=self._thread_name,
            daemon=True,
        )
        with self._lock:
            self._thread = thread
        try:
            thread.start()
        except RuntimeError as exc:
            self._persist_terminal(
                session_directory,
                state,
                AnalysisJobStatus.FAILED,
                error_message=str(exc),
            )
            self._clear()
            raise

    def cancel(self) -> None:
        with self._lock:
            self._cancel_requested = True
            process = self._process
        if process is not None and process.poll() is None:
            terminate_process_tree(process)

    def wait(self, timeout_seconds: float | None = None) -> bool:
        with self._lock:
            thread = self._thread
        if thread is None or thread is threading.current_thread():
            return not self.is_running
        thread.join(timeout_seconds)
        return not thread.is_alive()

    def shutdown(self, timeout_seconds: float | None = None) -> bool:
        self.cancel()
        return self.wait(timeout_seconds)

    def _run(
        self,
        session_directory: Path,
        state: AnalysisJobState,
        command: Sequence[str],
        environment_factory: EnvironmentFactory | None,
    ) -> None:
        session_value = str(session_directory)
        self._callbacks.started(session_value)
        try:
            environment = (
                dict(environment_factory())
                if environment_factory is not None
                else os.environ.copy()
            )
            environment["PYTHONUTF8"] = "1"
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
        except (OSError, RuntimeError) as exc:
            self._finish_failed(
                session_directory,
                state,
                f"{self._display_name}を開始できません: {exc}",
            )
            return

        with self._lock:
            self._process = process
            cancel_requested = self._cancel_requested
        if cancel_requested:
            terminate_process_tree(process)

        try:
            output_path, diagnostics = self._read_output(process)
            exit_code = process.wait()
        except Exception as exc:
            if process.poll() is None:
                terminate_process_tree(process)
            self._finish_failed(
                session_directory,
                state,
                f"{self._display_name}の実行中にエラーが発生しました: {exc}",
            )
            return

        with self._lock:
            canceled = self._cancel_requested
        if canceled:
            self._finish_canceled(session_directory, state)
        elif exit_code == 0 and output_path is not None:
            self._finish_succeeded(session_directory, state, output_path)
        else:
            detail = diagnostics[-1] if diagnostics else f"終了コード {exit_code}"
            self._finish_failed(
                session_directory,
                state,
                f"{self._display_name}に失敗しました: {detail}",
            )

    def _read_output(
        self,
        process: subprocess.Popen[str],
    ) -> tuple[str | None, deque[str]]:
        output_path: str | None = None
        diagnostics: deque[str] = deque(maxlen=20)
        assert process.stdout is not None
        for raw_line in process.stdout:
            line = raw_line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                diagnostics.append(line)
                continue
            if not isinstance(value, dict):
                continue
            if value.get("type") == "progress":
                percent = value.get("percent")
                message = value.get("message")
                if isinstance(percent, int) and isinstance(message, str):
                    self._callbacks.progress(percent, message)
            elif value.get("type") == "result" and isinstance(value.get("path"), str):
                output_path = value["path"]
        return output_path, diagnostics

    def _finish_succeeded(
        self,
        session_directory: Path,
        state: AnalysisJobState,
        output_path: str,
    ) -> None:
        try:
            relative_path = _relative_output_path(session_directory, output_path)
        except ValueError as exc:
            self._finish_failed(session_directory, state, str(exc))
            return
        persistence_error = self._persist_terminal(
            session_directory,
            state,
            AnalysisJobStatus.SUCCEEDED,
            output_path=relative_path,
        )
        self._clear()
        if persistence_error is None:
            self._callbacks.finished(str(session_directory), output_path)
        else:
            self._callbacks.failed(str(session_directory), persistence_error)

    def _finish_canceled(self, session_directory: Path, state: AnalysisJobState) -> None:
        persistence_error = self._persist_terminal(
            session_directory,
            state,
            AnalysisJobStatus.CANCELED,
        )
        self._clear()
        if persistence_error is None:
            self._callbacks.canceled(str(session_directory))
        else:
            self._callbacks.failed(str(session_directory), persistence_error)

    def _finish_failed(
        self,
        session_directory: Path,
        state: AnalysisJobState,
        message: str,
    ) -> None:
        persistence_error = self._persist_terminal(
            session_directory,
            state,
            AnalysisJobStatus.FAILED,
            error_message=message,
        )
        self._clear()
        self._callbacks.failed(
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
            self._repository.save(
                session_directory,
                state.finish(
                    status,
                    output_path=output_path,
                    error_message=error_message,
                ),
            )
        except OSError as exc:
            return f"{self._display_name}状態を保存できません: {exc}"
        return None

    def _clear(self) -> None:
        with self._lock:
            self._process = None
            self._thread = None
            self._cancel_requested = False
            self._running = False


def _relative_output_path(session_directory: Path, value: str) -> str:
    path = Path(value).resolve()
    try:
        return path.relative_to(session_directory.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError("workerの出力先がセッションフォルダ外です") from exc


def _join_errors(message: str, persistence_error: str | None) -> str:
    return message if persistence_error is None else f"{message} / {persistence_error}"
