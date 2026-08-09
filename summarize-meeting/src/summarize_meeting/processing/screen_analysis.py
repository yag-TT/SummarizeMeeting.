from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

import numpy as np

from summarize_meeting.domain.screen_analysis import OcrLine, ScreenRecognition
from summarize_meeting.domain.session import SESSION_SCHEMA_VERSION

ProgressCallback = Callable[[int, str], None]

_PADDLE_MODELS = (
    # The last value is the SHA-256 from Hugging Face's LFS oid, not the Xet hash.
    (
        "PP-OCRv6_medium_det",
        "PaddlePaddle/PP-OCRv6_medium_det_onnx",
        "61323801669c338b7891481ec7bac61ce31b576a",
        "eb13b44b25bb36f89528b68720af8a61d9cf381176107f465db1757b65d086e1",
    ),
    (
        "PP-OCRv6_medium_rec",
        "PaddlePaddle/PP-OCRv6_medium_rec_onnx",
        "50c7eacafc52fa7bcf4194e8cd08e46f8558504b",
        "9c09abf0957f7968c7586464b7397b84ad2387a0497a351af40e9acc71b673ba",
    ),
)


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


class PaddleOcrBackend:
    def __init__(self, *, models_directory: Path, language: str = "ja") -> None:
        self._models_directory = models_directory
        self._language = language
        self._engine: object | None = None

    @property
    def runtime_name(self) -> str:
        return "paddleocr-3.7/PP-OCRv6-medium-onnx"

    @property
    def language(self) -> str:
        return self._language

    def prepare(self) -> None:
        self._get_engine()

    def analyze(self, image_path: Path) -> ScreenRecognition:
        engine = self._get_engine()
        try:
            results = engine.predict(str(image_path))
            result = next(iter(results), None)
        except Exception as exc:
            raise ScreenAnalysisError(f"PaddleOCRの解析に失敗しました: {exc}") from exc
        if result is None:
            return ScreenRecognition(text="", lines=(), language=self._language)
        return _convert_paddle_result(result, language=self._language)

    def _get_engine(self):
        if self._engine is not None:
            return self._engine
        try:
            model_directories = ensure_paddle_models(self._models_directory)
            _configure_paddle_model_cache(self._models_directory)
            from paddleocr import PaddleOCR

            self._engine = PaddleOCR(
                text_detection_model_name="PP-OCRv6_medium_det",
                text_recognition_model_name="PP-OCRv6_medium_rec",
                text_detection_model_dir=str(model_directories["PP-OCRv6_medium_det"]),
                text_recognition_model_dir=str(model_directories["PP-OCRv6_medium_rec"]),
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
                engine="onnxruntime",
                device="cpu",
            )
        except Exception as exc:
            raise ScreenAnalysisError(
                "PaddleOCRモデルを準備できません。オンライン環境で "
                "uv run python scripts/setup_models.py ocr を実行してください: "
                f"{exc}"
            ) from exc
        return self._engine


def create_screen_analysis_backend(
    *,
    models_directory: Path,
    language: str = "ja",
) -> ScreenAnalysisBackend:
    return PaddleOcrBackend(models_directory=models_directory, language=language)


def default_screen_analysis_runtime() -> str:
    return "paddleocr-3.7/PP-OCRv6-medium-onnx"


def _configure_paddle_model_cache(directory: Path) -> None:
    resolved = str(directory.resolve())
    os.environ.setdefault("PADDLE_OCR_BASE_DIR", resolved)
    os.environ.setdefault("PADDLE_PDX_CACHE_HOME", resolved)
    os.environ.setdefault("HF_HOME", str((directory / "huggingface").resolve()))


def ensure_paddle_models(
    models_directory: Path,
    *,
    force: bool = False,
) -> dict[str, Path]:
    from huggingface_hub import snapshot_download

    models_directory.mkdir(parents=True, exist_ok=True)
    result: dict[str, Path] = {}
    for name, repository, revision, expected_sha256 in _PADDLE_MODELS:
        destination = models_directory / name
        model_path = destination / "inference.onnx"
        if force or not _matches_sha256(model_path, expected_sha256):
            snapshot_download(
                repo_id=repository,
                revision=revision,
                local_dir=destination,
                allow_patterns=("inference.onnx", "inference.json", "inference.yml", "README.md"),
                force_download=force,
            )
        if not _matches_sha256(model_path, expected_sha256):
            raise ScreenAnalysisError(
                f"PaddleOCRモデルのSHA-256が一致しません: {model_path}"
            )
        result[name] = destination
    return result


def paddle_models_status(models_directory: Path) -> dict[str, bool]:
    return {
        name: _matches_sha256(models_directory / name / "inference.onnx", expected_sha256)
        for name, _repository, _revision, expected_sha256 in _PADDLE_MODELS
    }


def _matches_sha256(path: Path, expected: str) -> bool:
    if not path.is_file():
        return False
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().casefold() == expected.casefold()


def _convert_paddle_result(value: object, *, language: str) -> ScreenRecognition:
    payload = _paddle_result_payload(value)
    texts = payload.get("rec_texts")
    polygons = payload.get("rec_polys", payload.get("dt_polys"))
    scores = payload.get("rec_scores")
    if not isinstance(texts, Sequence) or isinstance(texts, str | bytes):
        raise ScreenAnalysisError("PaddleOCR結果にrec_textsがありません")
    if not isinstance(polygons, Sequence | np.ndarray):
        raise ScreenAnalysisError("PaddleOCR結果に文字領域がありません")
    score_values = scores if isinstance(scores, Sequence | np.ndarray) else ()
    lines: list[OcrLine] = []
    for index, (text_value, polygon_value) in enumerate(zip(texts, polygons, strict=False)):
        text = str(text_value).strip()
        if not text:
            continue
        score = float(score_values[index]) if index < len(score_values) else 1.0
        if not math.isfinite(score) or score < 0:
            continue
        polygon = np.asarray(polygon_value, dtype=np.float64).reshape(-1, 2)
        if polygon.size == 0 or not np.isfinite(polygon).all():
            continue
        left = float(polygon[:, 0].min())
        top = float(polygon[:, 1].min())
        right = float(polygon[:, 0].max())
        bottom = float(polygon[:, 1].max())
        lines.append(
            OcrLine(
                text=text,
                x=round(left, 2),
                y=round(top, 2),
                width=round(max(0.0, right - left), 2),
                height=round(max(0.0, bottom - top), 2),
                confidence=score,
            )
        )
    return ScreenRecognition(
        text="\n".join(line.text for line in lines),
        lines=tuple(lines),
        language=language,
    )


def _paddle_result_payload(value: object) -> dict[str, object]:
    raw = getattr(value, "json", value)
    if callable(raw):
        raw = raw()
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ScreenAnalysisError("PaddleOCR結果のJSONが不正です") from exc
    if not isinstance(raw, dict):
        raise ScreenAnalysisError("PaddleOCR結果の形式が不正です")
    nested = raw.get("res")
    return nested if isinstance(nested, dict) else raw


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
        if session.get("schema_version") != SESSION_SCHEMA_VERSION:
            raise ScreenAnalysisError("現在のデータ形式ではないため画面解析できません")
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
