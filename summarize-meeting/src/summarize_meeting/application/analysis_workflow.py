"""複数の録音後解析ジョブを相互排他で調停する。"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class AnalysisControllerPort(Protocol):
    @property
    def is_running(self) -> bool: ...

    def cancel(self) -> None: ...

    def wait(self, timeout_seconds: float | None = None) -> bool: ...


class AnalysisSessionPort(Protocol):
    path: Path
    can_transcribe: bool
    can_diarize: bool
    can_analyze_screens: bool
    can_generate_minutes: bool
    transcription_status: str
    screen_analysis_status: str
    minutes_status: str


@dataclass(frozen=True, slots=True)
class AnalysisAvailability:
    transcribe: bool = False
    diarize: bool = False
    analyze_screens: bool = False
    generate_minutes: bool = False
    open_session: bool = False
    open_transcript: bool = False
    open_screen_analysis: bool = False
    open_minutes: bool = False


class AnalysisWorkflow:
    """同時起動を防ぎ、終了時は全workerの停止完了を待つ。"""

    def __init__(self, controllers: Iterable[AnalysisControllerPort | None]) -> None:
        self._controllers = tuple(
            controller for controller in controllers if controller is not None
        )
        self._start_lock = threading.RLock()

    @property
    def any_running(self) -> bool:
        return any(controller.is_running for controller in self._controllers)

    def other_running(self, current: object) -> bool:
        return any(
            controller is not current and controller.is_running
            for controller in self._controllers
        )

    def start(self, current: object, action: Callable[[], None]) -> None:
        with self._start_lock:
            if self.other_running(current):
                raise RuntimeError("別の解析処理が実行中です")
            action()

    def cancel_all(self) -> None:
        for controller in self._controllers:
            controller.cancel()

    def shutdown(self, timeout_seconds: float) -> bool:
        self.cancel_all()
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        completed = True
        for controller in self._controllers:
            wait = getattr(controller, "wait", None)
            if wait is None:
                completed = completed and not controller.is_running
                continue
            remaining = max(0.0, deadline - time.monotonic())
            completed = bool(wait(remaining)) and completed
        return completed

    def availability(
        self,
        summary: AnalysisSessionPort | None,
        *,
        transcription: object | None,
        diarization: object | None,
        screen_analysis: object | None,
        minutes: object | None,
    ) -> AnalysisAvailability:
        if summary is None:
            return AnalysisAvailability()
        return AnalysisAvailability(
            transcribe=_can_run(
                transcription,
                summary.can_transcribe,
                self.other_running(transcription),
            ),
            diarize=_can_run(
                diarization,
                summary.can_diarize,
                self.other_running(diarization),
            ),
            analyze_screens=_can_run(
                screen_analysis,
                summary.can_analyze_screens,
                self.other_running(screen_analysis),
            ),
            generate_minutes=_can_run(
                minutes,
                summary.can_generate_minutes
                and bool(getattr(minutes, "is_configured", False)),
                self.other_running(minutes),
            ),
            open_session=summary.path.exists(),
            open_transcript=(
                summary.transcription_status == "SUCCEEDED"
                and (summary.path / "output" / "transcript.md").is_file()
            ),
            open_screen_analysis=(
                summary.screen_analysis_status == "SUCCEEDED"
                and (summary.path / "analysis" / "screens.json").is_file()
            ),
            open_minutes=(
                summary.minutes_status == "SUCCEEDED"
                and (summary.path / "output" / "minutes.md").is_file()
            ),
        )


def _can_run(controller: object | None, ready: bool, other_running: bool) -> bool:
    return (
        controller is not None
        and (bool(getattr(controller, "is_running", False)) or ready)
        and not other_running
    )
