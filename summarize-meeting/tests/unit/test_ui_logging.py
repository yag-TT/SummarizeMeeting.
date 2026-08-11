from __future__ import annotations

import logging

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
