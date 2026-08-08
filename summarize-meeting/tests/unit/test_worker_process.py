from __future__ import annotations

from dataclasses import dataclass, field

import psutil

from summarize_meeting.application import worker_process


@dataclass
class _Process:
    children_value: list[_Process] = field(default_factory=list)
    terminated: int = 0
    killed: int = 0

    def children(self, *, recursive: bool) -> list[_Process]:
        assert recursive is True
        return self.children_value

    def terminate(self) -> None:
        self.terminated += 1

    def kill(self) -> None:
        self.killed += 1


@dataclass
class _Popen:
    pid: int = 42
    terminated: int = 0

    def terminate(self) -> None:
        self.terminated += 1


def test_terminate_process_tree_stops_descendants_and_parent(monkeypatch) -> None:
    child = _Process()
    parent = _Process([child])
    popen = _Popen()
    monkeypatch.setattr(worker_process.psutil, "Process", lambda pid: parent)
    monkeypatch.setattr(
        worker_process.psutil,
        "wait_procs",
        lambda processes, timeout: (list(processes), []),
    )

    worker_process.terminate_process_tree(popen, timeout=0.1)  # type: ignore[arg-type]

    assert child.terminated == 1
    assert parent.terminated == 1
    assert child.killed == 0
    assert parent.killed == 0


def test_terminate_process_tree_kills_survivors(monkeypatch) -> None:
    child = _Process()
    parent = _Process([child])
    waits = 0

    def wait_procs(processes, timeout):
        nonlocal waits
        waits += 1
        values = list(processes)
        return ([], [child]) if waits == 1 else (values, [])

    monkeypatch.setattr(worker_process.psutil, "Process", lambda pid: parent)
    monkeypatch.setattr(worker_process.psutil, "wait_procs", wait_procs)

    worker_process.terminate_process_tree(_Popen(), timeout=0.1)  # type: ignore[arg-type]

    assert child.killed == 1
    assert parent.killed == 0
    assert waits == 2


def test_terminate_process_tree_ignores_exited_process(monkeypatch) -> None:
    def missing(_pid):
        raise psutil.NoSuchProcess(42)

    monkeypatch.setattr(worker_process.psutil, "Process", missing)
    popen = _Popen()

    worker_process.terminate_process_tree(popen)  # type: ignore[arg-type]

    assert popen.terminated == 0
