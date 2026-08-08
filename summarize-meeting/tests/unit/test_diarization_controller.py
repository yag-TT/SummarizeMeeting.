from __future__ import annotations

import json
from pathlib import Path

from summarize_meeting.application.diarization_controller import DiarizationController
from summarize_meeting.infrastructure.paths import PortableAppPaths


class _ImmediateThread:
    def __init__(self, *, target, args, **_kwargs) -> None:
        self._target = target
        self._args = args

    def start(self) -> None:
        self._target(*self._args)


class _SuccessfulProcess:
    def __init__(self, output_path: Path, commands: list[list[str]]) -> None:
        self.stdout = iter(
            [
                '{"type":"progress","percent":100,"message":"完了"}\n',
                json.dumps({"type": "result", "path": str(output_path)}) + "\n",
            ]
        )
        self._commands = commands

    def poll(self) -> int:
        return 0

    def wait(self) -> int:
        return 0

    def terminate(self) -> None:
        pass


def _create_models(app_paths: PortableAppPaths) -> None:
    root = app_paths.models_dir / "sherpa-onnx" / "diarization"
    segmentation = root / "segmentation" / "model.int8.onnx"
    embedding = root / "embedding" / "nemo_en_titanet_small.onnx"
    segmentation.parent.mkdir(parents=True)
    embedding.parent.mkdir(parents=True)
    segmentation.write_bytes(b"model")
    embedding.write_bytes(b"model")


def test_controller_persists_success_and_speaker_count(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app_paths = PortableAppPaths(tmp_path / "app")
    app_paths.ensure_writable()
    _create_models(app_paths)
    session = app_paths.meetings_dir / "session"
    (session / "output").mkdir(parents=True)
    output = session / "output" / "transcript.md"
    commands: list[list[str]] = []
    monkeypatch.setattr(
        "summarize_meeting.application.diarization_controller.threading.Thread",
        _ImmediateThread,
    )

    def create_process(command, **_kwargs):
        commands.append(command)
        return _SuccessfulProcess(output, commands)

    monkeypatch.setattr(
        "summarize_meeting.application.diarization_controller.subprocess.Popen",
        create_process,
    )
    controller = DiarizationController(app_paths)
    finished: list[tuple[str, str]] = []
    controller.job_finished.connect(
        lambda session_path, path: finished.append((session_path, path))
    )

    controller.start(session, speaker_count=3)

    value = json.loads((session / "analysis" / "jobs.json").read_text(encoding="utf-8"))
    state = value["jobs"]["diarization"]
    assert state["status"] == "SUCCEEDED"
    assert state["output_path"] == "output/transcript.md"
    assert commands and commands[0][-2:] == ["--speaker-count", "3"]
    assert finished == [(str(session.resolve()), str(output))]
    assert not controller.is_running


def test_controller_persists_worker_start_failure(tmp_path: Path, monkeypatch) -> None:
    app_paths = PortableAppPaths(tmp_path / "app")
    app_paths.ensure_writable()
    _create_models(app_paths)
    session = app_paths.meetings_dir / "session"
    session.mkdir()
    monkeypatch.setattr(
        "summarize_meeting.application.diarization_controller.threading.Thread",
        _ImmediateThread,
    )

    def fail_start(*_args, **_kwargs):
        raise OSError("worker unavailable")

    monkeypatch.setattr(
        "summarize_meeting.application.diarization_controller.subprocess.Popen",
        fail_start,
    )
    controller = DiarizationController(app_paths)
    failures: list[str] = []
    controller.job_failed.connect(lambda _session, message: failures.append(message))

    controller.start(session)

    value = json.loads((session / "analysis" / "jobs.json").read_text(encoding="utf-8"))
    state = value["jobs"]["diarization"]
    assert state["status"] == "FAILED"
    assert "worker unavailable" in state["error_message"]
    assert "worker unavailable" in failures[0]
    assert not controller.is_running
