from __future__ import annotations

import json
from pathlib import Path

from summarize_meeting.application.minutes_controller import MinutesController
from summarize_meeting.infrastructure.paths import PortableAppPaths


class _ImmediateThread:
    def __init__(self, *, target, args, **_kwargs) -> None:
        self._target = target
        self._args = args

    def start(self) -> None:
        self._target(*self._args)


class _SuccessfulProcess:
    pid = 1234

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


def _setup(tmp_path: Path) -> tuple[PortableAppPaths, Path]:
    paths = PortableAppPaths(tmp_path / "app")
    paths.ensure_writable()
    session = paths.meetings_dir / "session"
    analysis = session / "analysis"
    analysis.mkdir(parents=True)
    (analysis / "transcription.json").write_text(
        '{"status":"SUCCEEDED","segments":[]}', encoding="utf-8"
    )
    return paths, session


def test_controller_persists_success(tmp_path: Path, monkeypatch) -> None:
    paths, session = _setup(tmp_path)
    output = session / "output" / "minutes.md"
    monkeypatch.setattr(
        "summarize_meeting.application.minutes_controller.threading.Thread",
        _ImmediateThread,
    )
    commands: list[list[str]] = []

    def start_process(command, **_kwargs):
        commands.append(command)
        return _SuccessfulProcess(output)

    monkeypatch.setattr(
        "summarize_meeting.application.minutes_controller.subprocess.Popen", start_process
    )
    controller = MinutesController(
        paths,
        base_url="http://127.0.0.1:2345/v1",
        model="existing-model",
    )
    finished: list[tuple[str, str]] = []
    controller.job_finished.connect(
        lambda session_path, path: finished.append((session_path, path))
    )

    controller.start(session)

    jobs = json.loads((session / "analysis" / "jobs.json").read_text(encoding="utf-8"))
    assert jobs["jobs"]["minutes"]["status"] == "SUCCEEDED"
    assert jobs["jobs"]["minutes"]["output_path"] == "output/minutes.md"
    assert finished == [(str(session.resolve()), str(output))]
    assert "http://127.0.0.1:2345/v1" in commands[0]
    assert "existing-model" in commands[0]
    assert not controller.is_running
