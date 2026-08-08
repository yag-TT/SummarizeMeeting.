from __future__ import annotations

import asyncio
import json
import math
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

import cv2
import numpy as np

from summarize_meeting.domain.screen_analysis import OcrLine, ScreenRecognition

ProgressCallback = Callable[[int, str], None]


class ScreenAnalysisError(RuntimeError):
    pass


class ScreenAnalysisBackend(Protocol):
    @property
    def runtime_name(self) -> str: ...

    @property
    def language(self) -> str: ...

    def analyze(self, image_path: Path) -> ScreenRecognition: ...


@dataclass(frozen=True, slots=True)
class ScreenshotEvent:
    sequence: int
    timestamp_ms: int
    file: str
    width: int
    height: int
    reason: str
    metrics: dict[str, float]


class WindowsOcrBackend:
    def __init__(self, *, language: str = "ja") -> None:
        self._language = language
        self._engine: object | None = None

    @property
    def runtime_name(self) -> str:
        return "windows-media-ocr"

    @property
    def language(self) -> str:
        return self._language

    def analyze(self, image_path: Path) -> ScreenRecognition:
        engine = self._get_engine()
        frame = _decode_image(image_path)
        height, width = frame.shape[:2]
        try:
            from winrt.windows.graphics.imaging import (
                BitmapAlphaMode,
                BitmapPixelFormat,
                SoftwareBitmap,
            )
            from winrt.windows.storage.streams import Buffer
        except ImportError as exc:  # pragma: no cover - required at runtime
            raise ScreenAnalysisError("Windows OCR projectionを読み込めません") from exc

        bgra = cv2.cvtColor(frame, cv2.COLOR_BGR2BGRA)
        raw = bgra.tobytes()
        buffer = Buffer(len(raw))
        buffer.length = len(raw)
        memoryview(buffer)[:] = raw
        bitmap = SoftwareBitmap(
            BitmapPixelFormat.BGRA8,
            width,
            height,
            BitmapAlphaMode.IGNORE,
        )
        bitmap.copy_from_buffer(buffer)
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(engine.recognize_async(bitmap))
        except Exception as exc:
            raise ScreenAnalysisError(f"Windows OCRに失敗しました: {exc}") from exc
        finally:
            loop.close()
            bitmap.close()
        lines = tuple(_convert_ocr_line(line) for line in result.lines if line.text.strip())
        return ScreenRecognition(
            text=result.text.strip(),
            lines=lines,
            language=self._language,
        )

    def _get_engine(self):
        if self._engine is not None:
            return self._engine
        try:
            from winrt.windows.globalization import Language
            from winrt.windows.media.ocr import OcrEngine
        except ImportError as exc:  # pragma: no cover - required at runtime
            raise ScreenAnalysisError("Windows OCR projectionを読み込めません") from exc
        language = Language(self._language)
        if not OcrEngine.is_language_supported(language):
            available = ", ".join(
                value.language_tag for value in OcrEngine.available_recognizer_languages
            )
            raise ScreenAnalysisError(
                f"Windows OCR言語パック {self._language} がありません"
                + (f"（利用可能: {available}）" if available else "")
            )
        self._engine = OcrEngine.try_create_from_language(language)
        if self._engine is None:
            raise ScreenAnalysisError(f"Windows OCR {self._language} を初期化できません")
        return self._engine


class ScreenAnalysisService:
    def __init__(self, backend: ScreenAnalysisBackend) -> None:
        self._backend = backend

    def run(
        self,
        session_directory: Path,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> Path:
        session_directory = session_directory.resolve()
        session = _read_object(session_directory / "session.json", "session.json")
        if session.get("status") != "RECORDED":
            raise ScreenAnalysisError("録音完了セッションだけを画面解析できます")
        screenshots_directory = session_directory / "screenshots"
        events = _read_events(screenshots_directory / "events.jsonl")
        if not events:
            raise ScreenAnalysisError("解析対象のスクリーンショットがありません")

        _notify(progress_callback, 0, "スクリーンショットを確認しています")
        screens: list[dict[str, object]] = []
        warnings: list[str] = []
        succeeded = 0
        total = len(events)
        for index, event in enumerate(events, 1):
            _notify(
                progress_callback,
                5 + round((index - 1) / total * 88),
                f"画面を解析しています ({index}/{total})",
            )
            try:
                image_path = _resolve_image_path(screenshots_directory, event.file)
                recognition = self._backend.analyze(image_path)
                understanding = derive_screen_understanding(recognition)
                screens.append(
                    {
                        **_event_dict(event),
                        "status": "SUCCEEDED",
                        "type": understanding["type"],
                        "title": understanding["title"],
                        "summary": understanding["summary"],
                        "important": understanding["important"],
                        "ocr": {
                            "language": recognition.language,
                            "text": recognition.text,
                            "lines": [line.to_dict() for line in recognition.lines],
                        },
                        "error_message": None,
                    }
                )
                succeeded += 1
            except Exception as exc:
                message = f"{event.file}: {exc}"
                warnings.append(message)
                screens.append(
                    {
                        **_event_dict(event),
                        "status": "FAILED",
                        "type": "unknown",
                        "title": "",
                        "summary": "",
                        "important": [],
                        "ocr": {
                            "language": self._backend.language,
                            "text": "",
                            "lines": [],
                        },
                        "error_message": str(exc),
                    }
                )
        if succeeded == 0:
            detail = warnings[0] if warnings else "原因不明"
            raise ScreenAnalysisError(f"すべてのスクリーンショット解析に失敗しました: {detail}")

        _notify(progress_callback, 95, "画面解析結果を保存しています")
        value = {
            "schema_version": 1,
            "status": "SUCCEEDED",
            "completed_at": _now_iso(),
            "runtime": self._backend.runtime_name,
            "language": self._backend.language,
            "statistics": {
                "total": total,
                "succeeded": succeeded,
                "failed": total - succeeded,
            },
            "screens": screens,
            "warnings": warnings,
        }
        output = session_directory / "analysis" / "screens.json"
        _write_json_atomic(output, value)
        _notify(progress_callback, 100, "画面解析が完了しました")
        return output


def derive_screen_understanding(recognition: ScreenRecognition) -> dict[str, object]:
    lines = [line.text.strip() for line in recognition.lines if line.text.strip()]
    if not lines and recognition.text.strip():
        lines = [line.strip() for line in recognition.text.splitlines() if line.strip()]
    screen_type = _classify_screen(lines)
    title = lines[0][:120] if lines else ""
    summary_text = " ".join(lines[:5])
    summary = (
        f"画面内テキスト: {summary_text[:300]}"
        if summary_text
        else "画面内の文字を検出できませんでした"
    )
    important = _important_lines(lines)
    return {
        "type": screen_type,
        "title": title,
        "summary": summary,
        "important": important,
    }


def _classify_screen(lines: Sequence[str]) -> str:
    text = " ".join(lines).casefold()
    rules = (
        ("meeting", ("microsoft teams", "google meet", "参加者", "ミーティング")),
        ("presentation", ("powerpoint", "スライド", "プレゼンテーション")),
        ("spreadsheet", ("microsoft excel", "スプレッドシート", "セル", "数式バー")),
        ("document", ("microsoft word", "文書", "ページ")),
        ("code", ("visual studio", "github", "pull request", "ソースコード")),
        ("browser", ("google chrome", "microsoft edge", "url", "http://", "https://")),
    )
    for screen_type, keywords in rules:
        if any(keyword in text for keyword in keywords):
            return screen_type
    return "unknown"


def _important_lines(lines: Sequence[str]) -> list[str]:
    keywords = (
        "todo",
        "決定",
        "担当",
        "期限",
        "締切",
        "課題",
        "重要",
        "必須",
        "変更",
        "エラー",
        "失敗",
        "リスク",
    )
    date_pattern = re.compile(r"(?:\d{4}[/-])?\d{1,2}[/-]\d{1,2}|\d{1,2}月\d{1,2}日|来週|今週")
    values: list[str] = []
    for line in lines:
        normalized = line.casefold()
        if (
            any(keyword in normalized for keyword in keywords) or date_pattern.search(line)
        ) and line not in values:
            values.append(line[:300])
        if len(values) == 10:
            break
    return values


def _convert_ocr_line(value: object) -> OcrLine:
    words = tuple(value.words)
    if words:
        left = min(float(word.bounding_rect.x) for word in words)
        top = min(float(word.bounding_rect.y) for word in words)
        right = max(float(word.bounding_rect.x + word.bounding_rect.width) for word in words)
        bottom = max(float(word.bounding_rect.y + word.bounding_rect.height) for word in words)
    else:
        left = top = right = bottom = 0.0
    return OcrLine(
        text=value.text.strip(),
        x=round(left, 2),
        y=round(top, 2),
        width=round(max(0.0, right - left), 2),
        height=round(max(0.0, bottom - top), 2),
    )


def _decode_image(path: Path) -> np.ndarray:
    try:
        encoded = np.frombuffer(path.read_bytes(), dtype=np.uint8)
    except OSError as exc:
        raise ScreenAnalysisError(f"画像を読み込めません: {exc}") from exc
    frame = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    if frame is None or frame.size == 0:
        raise ScreenAnalysisError("画像をdecodeできません")
    return frame


def _read_events(path: Path) -> tuple[ScreenshotEvent, ...]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ScreenAnalysisError(f"画面イベントを読み込めません: {exc}") from exc
    events: list[ScreenshotEvent] = []
    sequences: set[int] = set()
    files: set[str] = set()
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ScreenAnalysisError(f"画面イベント{line_number}行目が不正です") from exc
        if not isinstance(value, dict):
            raise ScreenAnalysisError(f"画面イベント{line_number}行目が不正です")
        sequence = _positive_int(value.get("sequence"), "sequence")
        timestamp_ms = _non_negative_int(value.get("timestamp_ms"), "timestamp_ms")
        file = value.get("file")
        if not isinstance(file, str) or not file:
            raise ScreenAnalysisError("画面イベントのfileが不正です")
        _validate_image_filename(file)
        if sequence in sequences or file in files:
            raise ScreenAnalysisError("画面イベントのsequenceまたはfileが重複しています")
        sequences.add(sequence)
        files.add(file)
        metrics_value = value.get("metrics")
        metrics: dict[str, float] = {}
        if isinstance(metrics_value, dict):
            metrics = {
                key: float(item)
                for key, item in metrics_value.items()
                if isinstance(key, str)
                and isinstance(item, int | float)
                and not isinstance(item, bool)
                and math.isfinite(item)
            }
        events.append(
            ScreenshotEvent(
                sequence=sequence,
                timestamp_ms=timestamp_ms,
                file=file,
                width=_positive_int(value.get("width"), "width"),
                height=_positive_int(value.get("height"), "height"),
                reason=value.get("reason") if isinstance(value.get("reason"), str) else "unknown",
                metrics=metrics,
            )
        )
    events.sort(key=lambda item: (item.timestamp_ms, item.sequence, item.file))
    return tuple(events)


def _resolve_image_path(directory: Path, value: str) -> Path:
    _validate_image_filename(value)
    path = directory / value
    if not path.is_file():
        raise ScreenAnalysisError(f"スクリーンショットがありません: {Path(value).name}")
    return path


def _validate_image_filename(value: str) -> None:
    relative = Path(value)
    if (
        relative.is_absolute()
        or relative.parts != (relative.name,)
        or relative.suffix.casefold() not in {".png", ".jpg", ".jpeg", ".webp"}
    ):
        raise ScreenAnalysisError("スクリーンショットのfileが不正です")


def _event_dict(event: ScreenshotEvent) -> dict[str, object]:
    return {
        "sequence": event.sequence,
        "timestamp_ms": event.timestamp_ms,
        "timestamp": round(event.timestamp_ms / 1000, 3),
        "image": f"screenshots/{event.file}",
        "width": event.width,
        "height": event.height,
        "reason": event.reason,
        "metrics": event.metrics,
    }


def _read_object(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScreenAnalysisError(f"{label}を読み込めません: {exc}") from exc
    if not isinstance(value, dict):
        raise ScreenAnalysisError(f"{label}の形式が不正です")
    return value


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ScreenAnalysisError(f"{label}が不正です")
    return value


def _non_negative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ScreenAnalysisError(f"{label}が不正です")
    return value


def _write_json_atomic(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _notify(callback: ProgressCallback | None, percent: int, message: str) -> None:
    if callback is not None:
        callback(min(100, max(0, percent)), message)
