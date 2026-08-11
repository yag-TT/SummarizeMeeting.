from __future__ import annotations

import logging
from pathlib import Path

from summarize_meeting.infrastructure.logging_support import redact_log_text
from summarize_meeting.ui.main_window import MainWindow


class _MessageLabel:
    def __init__(self) -> None:
        self.text = ""
        self.style = ""

    def setText(self, value: str) -> None:
        self.text = value

    def setStyleSheet(self, value: str) -> None:
        self.style = value


class _Window:
    def __init__(self) -> None:
        self._message = _MessageLabel()

    def _register_sensitive_log_values(self) -> None:
        MainWindow._register_sensitive_log_values(self)


class _TextWidget:
    def __init__(self, value: str) -> None:
        self._value = value

    def text(self) -> str:
        return self._value


def test_show_warning_writes_warning_log(caplog) -> None:
    window = _Window()

    with caplog.at_level(logging.WARNING, logger="summarize_meeting.ui.main_window"):
        MainWindow.show_warning(window, "入力レベルが低下しました")

    assert "UI warning: 入力レベルが低下しました" in caplog.text


def test_show_error_writes_error_log_with_active_exception(caplog) -> None:
    window = _Window()

    with caplog.at_level(logging.ERROR, logger="summarize_meeting.ui.main_window"):
        try:
            raise RuntimeError("device disconnected")
        except RuntimeError:
            MainWindow.show_error(window, "録音に失敗しました")

    record = caplog.records[-1]
    assert record.getMessage() == "UI error: 録音に失敗しました"
    assert record.exc_info is not None
    assert record.exc_info[0] is RuntimeError


def test_show_error_registers_meeting_title_and_session_path_for_redaction(caplog) -> None:
    meeting_title = "confidential-meeting-42"
    session_path = Path("C:/meetings/confidential-meeting-42/session")
    window = _Window()
    window._title = _TextWidget(meeting_title)
    window._session_path = session_path

    with caplog.at_level(logging.ERROR, logger="summarize_meeting.ui.main_window"):
        MainWindow.show_error(window, f"保存に失敗しました: {session_path}")

    redacted = redact_log_text(caplog.records[-1].getMessage())
    assert meeting_title not in redacted
    assert str(session_path) not in redacted
    assert "[REDACTED]" in redacted
