from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections import deque
from collections.abc import Mapping
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
from summarize_meeting.processing.diarization import (
    DiarizationService,
    SherpaOnnxDiarizationBackend,
)
from summarize_meeting.processing.sherpa_runtime import (
    prepare_sherpa_onnx_environment,
)


class DiarizationController(QObject):
    job_started = Signal(str)
    job_progress = Signal(int, str)
    job_finished = Signal(str, str)
    job_failed = Signal(str, str)
    job_canceled = Signal(str)

    def __init__(
        self,
        app_paths: PortableAppPaths,
        *,
        cluster_threshold: float = 0.75,
        job_repository: FileAnalysisJobRepository | None = None,
    ) -> None:
        super().__init__()
        self._app_paths = app_paths
        self._cluster_threshold = cluster_threshold
        self._job_repository = job_repository or FileAnalysisJobRepository()
        self._lock = threading.RLock()
        self._process: subprocess.Popen[str] | None = None
        self._cancel_requested = False
        self._running = False

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def start(self, session_directory: Path, *, speaker_count: int | None = None) -> None:
        if speaker_count is not None and not 1 <= speaker_count <= 10:
            raise ValueError("話者数は1から10の範囲で指定してください")
        session_directory = session_directory.resolve()
        try:
            session_directory.relative_to(self._app_paths.meetings_dir.resolve())
        except ValueError as exc:
            raise ValueError("アプリのmeetingsフォルダ外は解析できません") from exc
        root = self._app_paths.models_dir / "sherpa-onnx" / "diarization"
        required_models = (
            root / "segmentation" / "model.int8.onnx",
            root / "embedding" / "nemo_en_titanet_small.onnx",
        )
        missing = [str(path) for path in required_models if not path.is_file()]
        if missing:
            raise RuntimeError(
                "話者分離モデルがありません: "
                + ", ".join(missing)
                + " / python scripts/setup_models.py diarization を実行してください"
            )
        state = AnalysisJobState.start(
            job="diarization",
            model="sherpa-onnx pyannote+titanet",
            language="language-independent",
        )
        with self._lock:
            if self._running:
                raise RuntimeError("話者分離は既に実行中です")
            self._cancel_requested = False
            self._running = True
        try:
            self._job_repository.save(session_directory, state)
        except OSError as exc:
            self._clear_process()
            raise RuntimeError(f"話者分離状態を保存できません: {exc}") from exc
        thread = threading.Thread(
            target=self._run_worker,
            args=(session_directory, state, speaker_count),
            name="diarization-job",
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

    def update_speaker_names(
        self,
        session_directory: Path,
        names: Mapping[str, str],
    ) -> Path:
        root = self._app_paths.models_dir / "sherpa-onnx" / "diarization"
        backend = SherpaOnnxDiarizationBackend(
            segmentation_model=root / "segmentation" / "model.int8.onnx",
            embedding_model=root / "embedding" / "nemo_en_titanet_small.onnx",
        )
        return DiarizationService(backend).update_speaker_names(session_directory, names)

    def _run_worker(
        self,
        session_directory: Path,
        state: AnalysisJobState,
        speaker_count: int | None,
    ) -> None:
        self.job_started.emit(str(session_directory))
        command = [
            sys.executable,
            "-m",
            "summarize_meeting.processing.diarization_worker",
            "--session",
            str(session_directory),
            "--models-dir",
            str(self._app_paths.models_dir),
            "--cluster-threshold",
            str(self._cluster_threshold),
        ]
        if speaker_count is not None:
            command.extend(["--speaker-count", str(speaker_count)])
        environment = os.environ.copy()
        environment["PYTHONUTF8"] = "1"
        try:
            environment = prepare_sherpa_onnx_environment(environment)
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
            message = f"話者分離を開始できません: {exc}"
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
            message = f"話者分離に失敗しました: {detail}"
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
            return f"話者分離状態を保存できません: {exc}"
        return None

    def _clear_process(self) -> None:
        with self._lock:
            self._process = None
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
