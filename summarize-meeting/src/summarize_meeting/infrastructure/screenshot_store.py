from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

import cv2

from summarize_meeting.capture.screen.base import BgrFrame


class ScreenshotStore:
    def __init__(self, screenshots_dir: Path) -> None:
        self._directory = screenshots_dir
        self._events = screenshots_dir / "events.jsonl"
        self._sequence = 0
        self._lock = threading.Lock()

    @property
    def count(self) -> int:
        return self._sequence

    def save(
        self,
        frame: BgrFrame,
        *,
        timestamp_ms: int,
        reason: str,
        metrics: dict[str, float],
    ) -> str:
        success, encoded = cv2.imencode(".png", frame)
        if not success:
            raise RuntimeError("PNG encoding failed")
        with self._lock:
            sequence = self._sequence + 1
            filename = f"{sequence:06d}.png"
            output = self._directory / filename
            temporary = output.with_suffix(".png.tmp")
            temporary.write_bytes(encoded.tobytes())
            os.replace(temporary, output)
            event: dict[str, Any] = {
                "schema_version": 1,
                "sequence": sequence,
                "timestamp_ms": timestamp_ms,
                "file": filename,
                "width": int(frame.shape[1]),
                "height": int(frame.shape[0]),
                "reason": reason,
                "metrics": metrics,
            }
            with self._events.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
                stream.write("\n")
                stream.flush()
            self._sequence = sequence
            return filename
