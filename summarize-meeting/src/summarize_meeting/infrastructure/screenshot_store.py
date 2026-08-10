"""スクリーンショットと対応する時系列イベントを検証付きで保存する。"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from summarize_meeting.capture.screen.base import BgrFrame


class ScreenshotSaveError(RuntimeError):
    pass


class ScreenshotStore:
    """PNGを原子的に保存した後、events.jsonlへ根拠メタデータを追記する。"""

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
        try:
            success, encoded = cv2.imencode(".png", frame)
            if not success:
                raise ScreenshotSaveError("PNG encoding failed")
            with self._lock:
                sequence = self._sequence + 1
                filename = f"{sequence:06d}.png"
                output = self._directory / filename
                temporary = output.with_suffix(".png.tmp")
                with temporary.open("wb") as stream:
                    stream.write(encoded.tobytes())
                    stream.flush()
                    os.fsync(stream.fileno())
                stored = np.frombuffer(temporary.read_bytes(), dtype=np.uint8)
                decoded = cv2.imdecode(stored, cv2.IMREAD_UNCHANGED)
                expected_size = (int(frame.shape[0]), int(frame.shape[1]))
                if decoded is None or decoded.shape[:2] != expected_size:
                    raise ScreenshotSaveError("PNG verification failed")
                os.replace(temporary, output)
                self._sequence = sequence
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
                    os.fsync(stream.fileno())
                return filename
        except ScreenshotSaveError:
            raise
        except Exception as exc:
            raise ScreenshotSaveError(f"スクリーンショットを保存できません: {exc}") from exc
