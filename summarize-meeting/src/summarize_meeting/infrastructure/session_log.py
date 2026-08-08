from __future__ import annotations

import json
import os
import re
import threading
import traceback
from collections.abc import Iterable, Mapping
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
_SENSITIVE_FIELD_NAMES = {
    "device_id",
    "device_name",
    "path",
    "root",
    "screen_title",
    "target",
    "title",
}
_REDACTED = "[REDACTED]"


class SessionLogWriter:
    def __init__(
        self,
        path: Path,
        *,
        session_id: str,
        sensitive_values: Iterable[str | Path | None] = (),
        minimum_level: str = "INFO",
    ) -> None:
        normalized_level = minimum_level.upper()
        if normalized_level not in _LEVELS:
            raise ValueError(f"Unsupported log level: {minimum_level}")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._session_id = session_id
        self._minimum_level = _LEVELS[normalized_level]
        self._sensitive_values: set[str] = set()
        self._lock = threading.RLock()
        self.add_sensitive_values(*sensitive_values)
        self._stream: TextIO | None = path.open("a", encoding="utf-8", newline="\n")
        self._write_error: Exception | None = None

    @property
    def write_error(self) -> Exception | None:
        return self._write_error

    def add_sensitive_values(self, *values: str | Path | None) -> None:
        with self._lock:
            for value in values:
                if value is None:
                    continue
                text = str(value)
                if text:
                    self._sensitive_values.add(text)

    def write(
        self,
        event: str,
        *,
        level: str = "INFO",
        timestamp_ms: int | None = None,
        **details: Any,
    ) -> bool:
        normalized_level = level.upper()
        if normalized_level not in _LEVELS:
            raise ValueError(f"Unsupported log level: {level}")
        if _LEVELS[normalized_level] < self._minimum_level:
            return True
        value: dict[str, Any] = {
            "timestamp": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "level": normalized_level,
            "event": event,
            "session_id": self._session_id,
        }
        if timestamp_ms is not None:
            value["timestamp_ms"] = max(0, int(timestamp_ms))
        if details:
            value["details"] = self._sanitize_mapping(details)
        return self._append(value)

    def write_exception(
        self,
        event: str,
        exception: BaseException,
        *,
        component: str | None = None,
        error_code: str | None = None,
        timestamp_ms: int | None = None,
    ) -> bool:
        details: dict[str, Any] = {
            "exception_type": type(exception).__name__,
            "message": str(exception),
            "stack_trace": "".join(
                traceback.format_exception(
                    type(exception),
                    exception,
                    exception.__traceback__,
                )
            ),
        }
        if component is not None:
            details["component"] = component
        if error_code is not None:
            details["error_code"] = error_code
        return self.write(
            event,
            level="ERROR",
            timestamp_ms=timestamp_ms,
            **details,
        )

    def close(self) -> None:
        with self._lock:
            stream = self._stream
            self._stream = None
            if stream is None:
                return
            with suppress(OSError):
                stream.flush()
                os.fsync(stream.fileno())
            with suppress(OSError):
                stream.close()

    def _append(self, value: Mapping[str, Any]) -> bool:
        with self._lock:
            if self._stream is None or self._write_error is not None:
                return False
            try:
                line = json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
                self._stream.write(line)
                self._stream.flush()
            except Exception as exc:
                self._write_error = exc
                return False
        return True

    def _sanitize_mapping(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return {str(key): self._sanitize_value(item, key=str(key)) for key, item in value.items()}

    def _sanitize_value(self, value: Any, *, key: str | None = None) -> Any:
        if key is not None and key.casefold() in _SENSITIVE_FIELD_NAMES:
            return _REDACTED
        if isinstance(value, Mapping):
            return self._sanitize_mapping(value)
        if isinstance(value, (list, tuple, set)):
            return [self._sanitize_value(item) for item in value]
        if isinstance(value, Path):
            return self._redact_text(str(value))
        if isinstance(value, str):
            return self._redact_text(value)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return self._redact_text(str(value))

    def _redact_text(self, value: str) -> str:
        redacted = value
        for sensitive in sorted(self._sensitive_values, key=len, reverse=True):
            if len(sensitive) < 4:
                redacted = re.sub(
                    rf"(?<!\w){re.escape(sensitive)}(?!\w)",
                    _REDACTED,
                    redacted,
                )
            else:
                redacted = redacted.replace(sensitive, _REDACTED)
        return redacted

    def __enter__(self) -> SessionLogWriter:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
