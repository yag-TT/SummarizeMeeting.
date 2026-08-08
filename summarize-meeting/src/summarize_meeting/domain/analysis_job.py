from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4


class AnalysisJobStatus(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


@dataclass(frozen=True, slots=True)
class AnalysisJobState:
    job: str
    status: AnalysisJobStatus
    attempt_id: str
    started_at: str
    ended_at: str | None
    model: str
    language: str
    output_path: str | None = None
    error_message: str | None = None

    @classmethod
    def start(cls, *, job: str, model: str, language: str) -> AnalysisJobState:
        return cls(
            job=job,
            status=AnalysisJobStatus.RUNNING,
            attempt_id=str(uuid4()),
            started_at=_now_iso(),
            ended_at=None,
            model=model,
            language=language,
        )

    def finish(
        self,
        status: AnalysisJobStatus,
        *,
        output_path: str | None = None,
        error_message: str | None = None,
    ) -> AnalysisJobState:
        if status not in {
            AnalysisJobStatus.SUCCEEDED,
            AnalysisJobStatus.FAILED,
            AnalysisJobStatus.CANCELED,
        }:
            raise ValueError("finish status must be terminal")
        return replace(
            self,
            status=status,
            ended_at=_now_iso(),
            output_path=output_path,
            error_message=error_message,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")
