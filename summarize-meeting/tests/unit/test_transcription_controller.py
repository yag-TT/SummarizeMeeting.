from __future__ import annotations

import json
from pathlib import Path

from summarize_meeting.application.transcription_controller import TranscriptionController
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


def test_controller_persists_running_and_succeeded_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app_paths = PortableAppPaths(tmp_path / "app")
    app_paths.ensure_writable()
    session = app_paths.meetings_dir / "session"
    (session / "output").mkdir(parents=True)
    output = session / "output" / "transcript.md"
    monkeypatch.setattr(
        "summarize_meeting.application.transcription_controller.threading.Thread",
        _ImmediateThread,
    )
    monkeypatch.setattr(
        "summarize_meeting.application.transcription_controller.subprocess.Popen",
        lambda *_args, **_kwargs: _SuccessfulProcess(output),
    )
    controller = TranscriptionController(app_paths)
    finished: list[tuple[str, str]] = []
    controller.job_finished.connect(
        lambda session_path, path: finished.append((session_path, path))
    )

    controller.start(session)

    value = json.loads((session / "analysis" / "jobs.json").read_text(encoding="utf-8"))
    state = value["jobs"]["transcription"]
    assert state["status"] == "SUCCEEDED"
    assert state["ended_at"] is not None
    assert state["output_path"] == "output/transcript.md"
    assert finished == [(str(session.resolve()), str(output))]
    assert not controller.is_running


def test_controller_persists_worker_start_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app_paths = PortableAppPaths(tmp_path / "app")
    app_paths.ensure_writable()
    session = app_paths.meetings_dir / "session"
    session.mkdir()
    monkeypatch.setattr(
        "summarize_meeting.application.transcription_controller.threading.Thread",
        _ImmediateThread,
    )

    def fail_start(*_args, **_kwargs):
        raise OSError("worker unavailable")

    monkeypatch.setattr(
        "summarize_meeting.application.transcription_controller.subprocess.Popen",
        fail_start,
    )
    controller = TranscriptionController(app_paths)
    failures: list[str] = []
    controller.job_failed.connect(lambda _session, message: failures.append(message))

    controller.start(session)

    value = json.loads((session / "analysis" / "jobs.json").read_text(encoding="utf-8"))
    state = value["jobs"]["transcription"]
    assert state["status"] == "FAILED"
    assert "worker unavailable" in state["error_message"]
    assert "worker unavailable" in failures[0]
    assert not controller.is_running
