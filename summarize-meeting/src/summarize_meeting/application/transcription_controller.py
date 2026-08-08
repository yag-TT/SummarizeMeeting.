from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from summarize_meeting.infrastructure.paths import PortableAppPaths


class TranscriptionController(QObject):
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
    ) -> None:
        super().__init__()
        self._app_paths = app_paths
        self._model_name = model_name
        self._language = language
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
        with self._lock:
            if self._running:
                raise RuntimeError("文字起こしは既に実行中です")
            self._session_directory = session_directory
            self._cancel_requested = False
            self._running = True
        thread = threading.Thread(
            target=self._run_worker,
            args=(session_directory,),
            name="transcription-job",
            daemon=True,
        )
        thread.start()

    def cancel(self) -> None:
        with self._lock:
            self._cancel_requested = True
            process = self._process
        if process is not None and process.poll() is None:
            process.terminate()

    def _run_worker(self, session_directory: Path) -> None:
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
        creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                creationflags=creation_flags,
            )
        except OSError as exc:
            self._clear_process()
            self.job_failed.emit(str(session_directory), f"文字起こしを開始できません: {exc}")
            return
        with self._lock:
            self._process = process
            cancel_requested = self._cancel_requested
        if cancel_requested:
            process.terminate()

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
            self.job_canceled.emit(str(session_directory))
        elif exit_code == 0 and output_path is not None:
            self.job_finished.emit(str(session_directory), output_path)
        else:
            detail = diagnostic_lines[-1] if diagnostic_lines else f"終了コード {exit_code}"
            self.job_failed.emit(str(session_directory), f"文字起こしに失敗しました: {detail}")

    def _clear_process(self) -> None:
        with self._lock:
            self._process = None
            self._session_directory = None
            self._cancel_requested = False
            self._running = False
