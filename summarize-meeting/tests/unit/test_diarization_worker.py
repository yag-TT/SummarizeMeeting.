from __future__ import annotations

import os

from summarize_meeting.processing import diarization_worker


def test_native_runtime_environment_reexecs_with_prepared_marker(monkeypatch) -> None:
    monkeypatch.delenv(diarization_worker._RUNTIME_PREPARED_ENV, raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/external")
    monkeypatch.setattr(
        diarization_worker,
        "prepare_sherpa_onnx_environment",
        lambda _environment: {"LD_LIBRARY_PATH": "/sherpa:/external"},
    )
    captured: dict[str, object] = {}

    def execve(executable, arguments, environment) -> None:
        captured.update(
            executable=executable,
            arguments=arguments,
            environment=environment,
        )

    monkeypatch.setattr(diarization_worker.os, "execve", execve)

    diarization_worker._ensure_native_runtime_environment()

    environment = captured["environment"]
    assert isinstance(environment, dict)
    assert environment[diarization_worker._RUNTIME_PREPARED_ENV] == "1"
    assert captured["executable"] == diarization_worker.sys.executable
    assert captured["arguments"][:3] == [
        diarization_worker.sys.executable,
        "-m",
        "summarize_meeting.processing.diarization_worker",
    ]


def test_native_runtime_environment_does_not_reexec_after_marker(monkeypatch) -> None:
    monkeypatch.setenv(diarization_worker._RUNTIME_PREPARED_ENV, "1")
    monkeypatch.setattr(
        diarization_worker,
        "prepare_sherpa_onnx_environment",
        lambda _environment: (_ for _ in ()).throw(AssertionError("unexpected prepare")),
    )
    monkeypatch.setattr(
        os,
        "execve",
        lambda *_args: (_ for _ in ()).throw(AssertionError("unexpected exec")),
    )

    diarization_worker._ensure_native_runtime_environment()
