from __future__ import annotations

import pytest

from summarize_meeting.application.analysis_workflow import AnalysisWorkflow


class _Controller:
    def __init__(self) -> None:
        self.is_running = False
        self.canceled = False
        self.waited = False

    def cancel(self) -> None:
        self.canceled = True
        self.is_running = False

    def wait(self, timeout_seconds: float | None = None) -> bool:
        self.waited = True
        return True


def test_workflow_prevents_second_analysis_start() -> None:
    first = _Controller()
    second = _Controller()
    workflow = AnalysisWorkflow((first, second))

    workflow.start(first, lambda: setattr(first, "is_running", True))

    with pytest.raises(RuntimeError, match="別の解析"):
        workflow.start(second, lambda: setattr(second, "is_running", True))

    assert not second.is_running


def test_workflow_shutdown_cancels_and_waits_every_controller() -> None:
    controllers = (_Controller(), _Controller())
    for controller in controllers:
        controller.is_running = True
    workflow = AnalysisWorkflow(controllers)

    assert workflow.shutdown(timeout_seconds=1.0)

    assert all(controller.canceled and controller.waited for controller in controllers)
