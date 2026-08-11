from __future__ import annotations

import json
import threading
from pathlib import Path

from summarize_meeting.application import analysis_job_runner as runner_module
from summarize_meeting.application.analysis_job_runner import (
    AnalysisJobCallbacks,
    AnalysisJobRunner,
)
from summarize_meeting.domain.analysis_job import AnalysisJobState
from summarize_meeting.infrastructure.analysis_job_repository import (
    FileAnalysisJobRepository,
)


class _BlockingProcess:
    def __init__(self) -> None:
        self.stdout = self
        self._released = threading.Event()
        self._iterated = False

    def __iter__(self):
        return self

    def __next__(self) -> str:
        if self._iterated:
            raise StopIteration
        self._iterated = True
        self._released.wait(timeout=2.0)
        raise StopIteration

    def poll(self) -> int | None:
        return 1 if self._released.is_set() else None

    def wait(self) -> int:
        return 1

    def release(self) -> None:
        self._released.set()


def test_runner_shutdown_cancels_process_and_waits_for_worker(
    tmp_path: Path,
    monkeypatch,
) -> None:
    process = _BlockingProcess()
    started = threading.Event()
    canceled: list[str] = []
    monkeypatch.setattr(runner_module.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(runner_module, "terminate_process_tree", lambda value: value.release())
    runner = AnalysisJobRunner(
        FileAnalysisJobRepository(),
        display_name="テスト解析",
        thread_name="test-analysis",
        callbacks=AnalysisJobCallbacks(
            started=lambda _path: started.set(),
            progress=lambda _percent, _message: None,
            finished=lambda _session, _output: None,
            failed=lambda _session, _message: None,
            canceled=canceled.append,
        ),
    )
    state = AnalysisJobState.start(job="test", model=None, language=None)

    runner.start(tmp_path, state, ["worker"])
    assert started.wait(timeout=1.0)

    assert runner.shutdown(timeout_seconds=1.0)

    saved = json.loads((tmp_path / "analysis" / "jobs.json").read_text("utf-8"))
    assert saved["jobs"]["test"]["status"] == "CANCELED"
    assert canceled == [str(tmp_path)]
    assert not runner.is_running


def test_runner_rejects_success_output_outside_session(
    tmp_path: Path,
    monkeypatch,
) -> None:
    outside = tmp_path.parent / "outside.md"

    class SuccessfulProcess:
        stdout = iter([json.dumps({"type": "result", "path": str(outside)}) + "\n"])

        @staticmethod
        def poll() -> int:
            return 0

        @staticmethod
        def wait() -> int:
            return 0

    monkeypatch.setattr(
        runner_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: SuccessfulProcess(),
    )
    failures: list[str] = []
    runner = AnalysisJobRunner(
        FileAnalysisJobRepository(),
        display_name="テスト解析",
        thread_name="test-analysis",
        callbacks=AnalysisJobCallbacks(
            started=lambda _path: None,
            progress=lambda _percent, _message: None,
            finished=lambda _session, _output: None,
            failed=lambda _session, message: failures.append(message),
            canceled=lambda _session: None,
        ),
    )
    state = AnalysisJobState.start(job="test", model=None, language=None)

    runner.start(tmp_path, state, ["worker"])
    assert runner.wait(timeout_seconds=1.0)

    saved = json.loads((tmp_path / "analysis" / "jobs.json").read_text("utf-8"))
    assert saved["jobs"]["test"]["status"] == "FAILED"
    assert "セッションフォルダ外" in failures[0]
