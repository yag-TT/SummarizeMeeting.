"""録音処理からセッションログの障害検知を分離する。"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from summarize_meeting.infrastructure.session_log import SessionLogWriter


class SessionLogMonitor:
    """writerの失敗を最初の1回だけアプリケーション層へ通知する。"""

    def __init__(
        self,
        writer: SessionLogWriter,
        on_write_failure: Callable[[Exception], None],
    ) -> None:
        self._writer = writer
        self._on_write_failure = on_write_failure
        self._failure_reported = False
        self._lock = threading.Lock()

    @classmethod
    def open(
        cls,
        path: Path,
        *,
        session_id: str,
        sensitive_values: Iterable[str | Path | None],
        minimum_level: str,
        on_write_failure: Callable[[Exception], None],
    ) -> SessionLogMonitor:
        return cls(
            SessionLogWriter(
                path,
                session_id=session_id,
                sensitive_values=sensitive_values,
                minimum_level=minimum_level,
            ),
            on_write_failure,
        )

    def add_sensitive_values(self, *values: str | Path | None) -> None:
        self._writer.add_sensitive_values(*values)

    def write(
        self,
        event: str,
        *,
        level: str = "INFO",
        timestamp_ms: int | None = None,
        **details: Any,
    ) -> bool:
        written = self._writer.write(
            event,
            level=level,
            timestamp_ms=timestamp_ms,
            **details,
        )
        self._report_failure(written)
        return written

    def write_exception(
        self,
        event: str,
        exception: BaseException,
        *,
        component: str | None = None,
        error_code: str | None = None,
        timestamp_ms: int | None = None,
    ) -> bool:
        written = self._writer.write_exception(
            event,
            exception,
            component=component,
            error_code=error_code,
            timestamp_ms=timestamp_ms,
        )
        self._report_failure(written)
        return written

    def close(self) -> None:
        self._writer.close()

    def _report_failure(self, written: bool) -> None:
        error = self._writer.write_error
        if written or error is None:
            return
        with self._lock:
            if self._failure_reported:
                return
            self._failure_reported = True
        self._on_write_failure(error)
