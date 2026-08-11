"""アプリケーションログへ出力する機密値を一元的にマスクする。"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Iterable
from pathlib import Path

_REDACTED = "[REDACTED]"
_sensitive_values: set[str] = set()
_sensitive_values_lock = threading.RLock()


def register_sensitive_log_values(*values: str | Path | None) -> None:
    """以降に整形される全アプリログで伏せる値を登録する。"""

    with _sensitive_values_lock:
        for value in values:
            if value is None:
                continue
            text = str(value)
            if text:
                _sensitive_values.add(text)


def redact_log_text(value: str, extra_values: Iterable[str | Path | None] = ()) -> str:
    sensitive_values = _sensitive_snapshot(extra_values)
    redacted = value
    for sensitive in sorted(sensitive_values, key=len, reverse=True):
        if len(sensitive) < 4:
            redacted = re.sub(
                rf"(?<!\w){re.escape(sensitive)}(?!\w)",
                _REDACTED,
                redacted,
            )
        else:
            redacted = redacted.replace(sensitive, _REDACTED)
    return redacted


class RedactingLogFormatter(logging.Formatter):
    """メッセージと例外tracebackを整形後にまとめてマスクする。"""

    def format(self, record: logging.LogRecord) -> str:
        return redact_log_text(super().format(record))


def _sensitive_snapshot(extra_values: Iterable[str | Path | None]) -> set[str]:
    with _sensitive_values_lock:
        values = set(_sensitive_values)
    values.update(str(value) for value in extra_values if value is not None and str(value))
    return values
