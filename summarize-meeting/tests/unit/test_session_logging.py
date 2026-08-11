from __future__ import annotations

from pathlib import Path

from summarize_meeting.application.session_logging import SessionLogMonitor
from summarize_meeting.infrastructure.session_log import SessionLogWriter


def test_session_log_monitor_reports_writer_failure_once(
    tmp_path: Path,
) -> None:
    writer = SessionLogWriter(tmp_path / "session.log", session_id="session")
    failures: list[Exception] = []
    monitor = SessionLogMonitor(writer, failures.append)

    assert not monitor.write("first", message="\udcff")
    assert not monitor.write("second")

    assert len(failures) == 1
    assert isinstance(failures[0], UnicodeEncodeError)
