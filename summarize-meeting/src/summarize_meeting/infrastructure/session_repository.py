from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from summarize_meeting.domain.session import RecordingSession

_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_title(value: str) -> str:
    sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value.strip())
    sanitized = re.sub(r"\s+", " ", sanitized).rstrip(". ")
    if not sanitized:
        sanitized = "会議"
    if sanitized.upper() in _WINDOWS_RESERVED:
        sanitized = f"_{sanitized}"
    return sanitized[:80]


@dataclass(frozen=True, slots=True)
class SessionPaths:
    root: Path
    logs: Path
    session_log: Path
    audio: Path
    screenshots: Path
    analysis: Path
    output: Path
    events: Path
    session_json: Path


class FileSessionRepository:
    def __init__(self, meetings_root: Path) -> None:
        self._meetings_root = meetings_root
        self._lock = threading.RLock()

    def create(self, session: RecordingSession) -> SessionPaths:
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        dirname = f"{timestamp}_{sanitize_title(session.title)}_{session.id[:8]}"
        root = self._meetings_root / dirname
        paths = SessionPaths(
            root=root,
            logs=root / "logs",
            session_log=root / "logs" / "session.log",
            audio=root / "audio",
            screenshots=root / "screenshots",
            analysis=root / "analysis",
            output=root / "output",
            events=root / "events.jsonl",
            session_json=root / "session.json",
        )
        for directory in (
            paths.audio,
            paths.logs,
            paths.screenshots,
            paths.analysis,
            paths.output,
        ):
            directory.mkdir(parents=True, exist_ok=False)
        self.save(paths, session)
        return paths

    def save(self, paths: SessionPaths, session: RecordingSession) -> None:
        with self._lock:
            self._write_json_atomic(paths.session_json, session.to_dict())

    def append_event(self, paths: SessionPaths, event: dict[str, Any]) -> None:
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock, paths.events.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(line)
            stream.flush()

    @staticmethod
    def _write_json_atomic(path: Path, value: Any) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
