"""話者分離workerの前提条件とQt通知を管理する。"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
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
from summarize_meeting.processing.diarization import (
    DiarizationService,
    SherpaOnnxDiarizationBackend,
)
from summarize_meeting.processing.sherpa_runtime import prepare_sherpa_onnx_environment


class DiarizationController(QObject):
    """モデル前提条件を確認し、話者分離workerを起動する。"""

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
        self._runner = AnalysisJobRunner(
            job_repository or FileAnalysisJobRepository(),
            display_name="話者分離",
            thread_name="diarization-job",
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

    def start(self, session_directory: Path, *, speaker_count: int | None = None) -> None:
        if speaker_count is not None and not 1 <= speaker_count <= 10:
            raise ValueError("話者数は1から10の範囲で指定してください")
        session_directory = self._validated_session(session_directory)
        root = self._model_root()
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
        self._runner.start(
            session_directory,
            state,
            command,
            environment_factory=lambda: prepare_sherpa_onnx_environment(
                os.environ.copy()
            ),
        )

    def cancel(self) -> None:
        self._runner.cancel()

    def wait(self, timeout_seconds: float | None = None) -> bool:
        return self._runner.wait(timeout_seconds)

    def shutdown(self, timeout_seconds: float | None = None) -> bool:
        return self._runner.shutdown(timeout_seconds)

    def update_speaker_names(
        self,
        session_directory: Path,
        names: Mapping[str, str],
    ) -> Path:
        session_directory = self._validated_session(session_directory)
        root = self._model_root()
        backend = SherpaOnnxDiarizationBackend(
            segmentation_model=root / "segmentation" / "model.int8.onnx",
            embedding_model=root / "embedding" / "nemo_en_titanet_small.onnx",
        )
        return DiarizationService(backend).update_speaker_names(session_directory, names)

    def _validated_session(self, session_directory: Path) -> Path:
        resolved = session_directory.resolve()
        try:
            resolved.relative_to(self._app_paths.meetings_dir.resolve())
        except ValueError as exc:
            raise ValueError("アプリのmeetingsフォルダ外は解析できません") from exc
        return resolved

    def _model_root(self) -> Path:
        return self._app_paths.models_dir / "sherpa-onnx" / "diarization"
