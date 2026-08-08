from __future__ import annotations

import json
from pathlib import Path

from summarize_meeting.application.screen_analysis_controller import (
    ScreenAnalysisController,
)
from summarize_meeting.infrastructure.paths import PortableAppPaths


class _ImmediateThread:
    def __init__(self, *, target, args, **_kwargs) -> None:
        self._target = target
        self._args = args

    def start(self) -> None:
        self._target(*self._args)


class _SuccessfulProcess:
    def __init__(self, output_path: Path) -> None:
        self.stdout = iter(
            [
                '{"type":"progress","percent":100,"message":"完了"}\n',
                json.dumps({"type": "result", "path": str(output_path)}) + "\n",
            ]
        )

    def poll(self) -> int:
        return 0

    def wait(self) -> int:
        return 0

    def terminate(self) -> None:
        pass


class _CancelingProcess:
    def __init__(self, cancel) -> None:
        self.stdout = self
        self._cancel = cancel
        self._iterated = False
        self.terminated = False

    def __iter__(self):
        return self

    def __next__(self) -> str:
        if self._iterated:
            raise StopIteration
        self._iterated = True
        self._cancel()
        raise StopIteration

    def poll(self) -> int | None:
        return 1 if self.terminated else None

    def wait(self) -> int:
        return 1

    def terminate(self) -> None:
        self.terminated = True


def _session(app_paths: PortableAppPaths) -> Path:
    session = app_paths.meetings_dir / "session"
    screenshots = session / "screenshots"
    screenshots.mkdir(parents=True)
    (screenshots / "events.jsonl").write_text("{}\n", encoding="utf-8")
    (session / "analysis").mkdir()
    return session


def test_controller_persists_success(tmp_path: Path, monkeypatch) -> None:
    app_paths = PortableAppPaths(tmp_path / "app")
    app_paths.ensure_writable()
    session = _session(app_paths)
    output = session / "analysis" / "screens.json"
    monkeypatch.setattr(
        "summarize_meeting.application.screen_analysis_controller.threading.Thread",
        _ImmediateThread,
    )
    monkeypatch.setattr(
        "summarize_meeting.application.screen_analysis_controller.subprocess.Popen",
        lambda *_args, **_kwargs: _SuccessfulProcess(output),
    )
    controller = ScreenAnalysisController(app_paths)
    finished: list[tuple[str, str]] = []
    controller.job_finished.connect(
        lambda session_path, path: finished.append((session_path, path))
    )

    controller.start(session)

    value = json.loads((session / "analysis" / "jobs.json").read_text(encoding="utf-8"))
    state = value["jobs"]["screen_analysis"]
    assert state["status"] == "SUCCEEDED"
    assert state["output_path"] == "analysis/screens.json"
    assert finished == [(str(session.resolve()), str(output))]
    assert not controller.is_running


def test_controller_persists_worker_start_failure(tmp_path: Path, monkeypatch) -> None:
    app_paths = PortableAppPaths(tmp_path / "app")
    app_paths.ensure_writable()
    session = _session(app_paths)
    monkeypatch.setattr(
        "summarize_meeting.application.screen_analysis_controller.threading.Thread",
        _ImmediateThread,
    )

    def fail_start(*_args, **_kwargs):
        raise OSError("worker unavailable")

    monkeypatch.setattr(
        "summarize_meeting.application.screen_analysis_controller.subprocess.Popen",
        fail_start,
    )
    controller = ScreenAnalysisController(app_paths)
    failures: list[str] = []
    controller.job_failed.connect(lambda _session, message: failures.append(message))

    controller.start(session)

    value = json.loads((session / "analysis" / "jobs.json").read_text(encoding="utf-8"))
    state = value["jobs"]["screen_analysis"]
    assert state["status"] == "FAILED"
    assert "worker unavailable" in state["error_message"]
    assert "worker unavailable" in failures[0]
    assert not controller.is_running


def test_controller_persists_canceled_state(tmp_path: Path, monkeypatch) -> None:
    app_paths = PortableAppPaths(tmp_path / "app")
    app_paths.ensure_writable()
    session = _session(app_paths)
    monkeypatch.setattr(
        "summarize_meeting.application.screen_analysis_controller.threading.Thread",
        _ImmediateThread,
    )
    controller = ScreenAnalysisController(app_paths)
    process = _CancelingProcess(controller.cancel)
    monkeypatch.setattr(
        "summarize_meeting.application.screen_analysis_controller.subprocess.Popen",
        lambda *_args, **_kwargs: process,
    )
    canceled: list[str] = []
    controller.job_canceled.connect(canceled.append)

    controller.start(session)

    value = json.loads((session / "analysis" / "jobs.json").read_text(encoding="utf-8"))
    assert value["jobs"]["screen_analysis"]["status"] == "CANCELED"
    assert canceled == [str(session.resolve())]
    assert process.terminated
    assert not controller.is_running
