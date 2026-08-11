"""文字起こしと画面解析を根拠付きタイムラインへ統合し、会議要約を生成する。"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

from summarize_meeting.domain.session import SESSION_SCHEMA_VERSION
from summarize_meeting.infrastructure.atomic_io import ArtifactPublisher, json_bytes

ProgressCallback = Callable[[int, str], None]

class MinutesError(RuntimeError):
    pass


class MinutesBackend(Protocol):
    @property
    def runtime_name(self) -> str: ...

    @property
    def model_name(self) -> str: ...

    def generate(self, prompt: str, schema: Mapping[str, object]) -> Mapping[str, object]: ...


class LlamaCppMinutesBackend:
    def __init__(
        self,
        *,
        base_url: str,
        model: str | None = None,
        max_output_tokens: int = 2_048,
        timeout_seconds: float = 600,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("llama.cpp APIには有効なHTTP(S)アドレスを指定してください")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._base_url = base_url.rstrip("/")
        self._model = model.strip() if isinstance(model, str) and model.strip() else None
        self._max_output_tokens = max_output_tokens
        self._timeout_seconds = timeout_seconds

    @property
    def runtime_name(self) -> str:
        return "llama.cpp OpenAI-compatible API"

    @property
    def model_name(self) -> str:
        return self._model or "auto"

    def generate(self, prompt: str, schema: Mapping[str, object]) -> Mapping[str, object]:
        model = self._model or self._resolve_model()
        self._model = model
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "入力資料だけを根拠に、会議・雑談・相談・インタビューなど"
                        "種類を問わない日本語の会話要約JSONを作成してください。"
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": self._max_output_tokens,
            "reasoning_effort": "none",
            "stream": False,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "conversation_summary",
                    "strict": True,
                    "schema": schema,
                },
            },
        }
        response = self._request_json("POST", "/chat/completions", payload)
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise MinutesError("llama.cpp APIの応答にchoicesがありません")
        message = choices[0].get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise MinutesError("llama.cpp APIの応答に会話要約JSONがありません")
        return _extract_json_object(content)

    def _resolve_model(self) -> str:
        response = self._request_json("GET", "/models")
        data = response.get("data")
        model_ids = [
            value.get("id")
            for value in data
            if isinstance(value, dict) and isinstance(value.get("id"), str)
        ] if isinstance(data, list) else []
        model_ids = list(dict.fromkeys(model_ids))
        if not model_ids:
            raise MinutesError(
                "llama.cppに利用可能なモデルがありません。モデルをロードしてください"
            )
        if len(model_ids) > 1:
            raise MinutesError(
                "llama.cppに複数モデルがあります。"
                "SUMMARIZE_MEETING_LLM_MODELで使用モデルを指定してください"
            )
        return model_ids[0]

    def _request_json(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(self._base_url + path, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                raw = response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            raise MinutesError(f"llama.cpp APIエラー ({exc.code}): {detail}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise MinutesError(
                "llama.cpp APIへ接続できません。llama.cpp serverを起動してください: "
                f"{exc}"
            ) from exc
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MinutesError("llama.cpp APIのJSON応答を解析できません") from exc
        if not isinstance(value, dict):
            raise MinutesError("llama.cpp APIの応答形式が不正です")
        return value


class MinutesService:
    """長い会議を分割要約し、根拠ID検証後にJSONとMarkdownを保存する。"""

    def __init__(
        self,
        backend: MinutesBackend,
        *,
        max_chunk_characters: int = 24_000,
    ) -> None:
        if max_chunk_characters < 2_000:
            raise ValueError("max_chunk_characters must be at least 2000")
        self._backend = backend
        self._max_chunk_characters = max_chunk_characters

    def run(
        self,
        session_directory: Path,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> Path:
        session_directory = session_directory.resolve()
        session = _read_object(session_directory / "session.json", "session.json")
        if session.get("schema_version") != SESSION_SCHEMA_VERSION:
            raise MinutesError("現在のデータ形式ではないため会話要約できません")
        if session.get("status") != "RECORDED":
            raise MinutesError("録音完了セッションだけを会話要約できます")

        _notify(progress_callback, 0, "文字起こしと画面解析結果を確認しています")
        transcript, transcript_path, transcript_mode = _load_transcript(session_directory)
        screens, screen_path, screen_warning = _load_screens(session_directory)
        warnings = [screen_warning] if screen_warning else []
        timeline = _build_timeline(
            session=session,
            transcript=transcript,
            transcript_path=transcript_path,
            transcript_mode=transcript_mode,
            screens=screens,
            screen_path=screen_path,
            warnings=warnings,
        )
        # コンテキスト上限を超える会議は部分要約し、最後に同じschemaで統合する。
        chunks = _timeline_chunks(timeline["items"], self._max_chunk_characters)
        partials: list[Mapping[str, object]] = []
        for index, chunk in enumerate(chunks, 1):
            start = 8 + round((index - 1) / len(chunks) * 70)
            _notify(
                progress_callback,
                start,
                f"会話の内容を整理しています ({index}/{len(chunks)})",
            )
            partials.append(
                self._backend.generate(
                    _generation_prompt(session, chunk, index=index, total=len(chunks)),
                    MINUTES_JSON_SCHEMA,
                )
            )
        if len(partials) == 1:
            generated = partials[0]
        else:
            _notify(progress_callback, 80, "分割した要約を統合しています")
            generated = self._backend.generate(
                _merge_prompt(session, partials),
                MINUTES_JSON_SCHEMA,
            )

        _notify(progress_callback, 90, "生成内容と根拠を検証しています")
        # LLMの各主張に、実在するtimeline項目の根拠IDが付いているか検証する。
        valid_ids = {
            str(item["id"]): item
            for item in timeline["items"]
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        validated, validation_warnings = _validate_generated(generated, valid_ids)
        warnings.extend(validation_warnings)
        generation_id = str(uuid4())
        completed_at = _now_iso()
        source_hash = hashlib.sha256(
            json.dumps(timeline, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        minutes_value = {
            "schema_version": 2,
            "status": "SUCCEEDED",
            "generation_id": generation_id,
            "completed_at": completed_at,
            "runtime": self._backend.runtime_name,
            "model": self._backend.model_name,
            "source": {
                "timeline": "analysis/timeline.json",
                "timeline_sha256": source_hash,
                "chunk_count": len(chunks),
            },
            "minutes": validated,
            "warnings": warnings,
        }
        _notify(progress_callback, 96, "会話要約を保存しています")
        output = session_directory / "output" / "minutes.md"
        ArtifactPublisher(session_directory).publish(
            {
                session_directory / "analysis" / "timeline.json": json_bytes(timeline),
                session_directory / "analysis" / "minutes.json": json_bytes(minutes_value),
                output: _render_markdown(session, timeline, validated, warnings).encode(
                    "utf-8"
                ),
            }
        )
        _notify(progress_callback, 100, "会話要約が完了しました")
        return output


def _evidenced_object(properties: Mapping[str, object]) -> dict[str, object]:
    all_properties = dict(properties)
    all_properties["evidence_ids"] = {"type": "array", "items": {"type": "string"}}
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": all_properties,
        "required": list(all_properties),
    }


MINUTES_JSON_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
        "topics": {
            "type": "array",
            "items": _evidenced_object(
                {"title": {"type": "string"}, "summary": {"type": "string"}}
            ),
        },
        "key_points": {
            "type": "array",
            "items": _evidenced_object({"text": {"type": "string"}}),
        },
        "decisions": {
            "type": "array",
            "items": _evidenced_object({"text": {"type": "string"}}),
        },
        "todos": {
            "type": "array",
            "items": _evidenced_object(
                {
                    "assignee": {"type": "string"},
                    "task": {"type": "string"},
                    "deadline": {"type": "string"},
                }
            ),
        },
        "pending": {
            "type": "array",
            "items": _evidenced_object({"text": {"type": "string"}}),
        },
        "references": {
            "type": "array",
            "items": _evidenced_object({"text": {"type": "string"}}),
        },
        "participants": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "summary",
        "topics",
        "key_points",
        "decisions",
        "todos",
        "pending",
        "references",
        "participants",
    ],
}


def _load_transcript(
    session_directory: Path,
) -> tuple[list[object], str, str]:
    candidates = (
        ("analysis/diarized_transcription.json", "diarized"),
        ("analysis/transcription.json", "source"),
    )
    for relative, mode in candidates:
        path = session_directory / relative
        if not path.is_file():
            continue
        value = _read_object(path, relative)
        segments = value.get("segments")
        if value.get("status") == "SUCCEEDED" and isinstance(segments, list):
            return segments, relative, mode
    raise MinutesError("成功済みの文字起こし結果が必要です")


def _load_screens(session_directory: Path) -> tuple[list[object], str | None, str | None]:
    path = session_directory / "analysis" / "screens.json"
    if not path.is_file():
        return [], None, "画面解析結果がないため、音声文字起こしだけから生成しました"
    value = _read_object(path, "analysis/screens.json")
    screens = value.get("screens")
    if value.get("status") != "SUCCEEDED" or not isinstance(screens, list):
        return [], None, "画面解析結果が成功状態でないため使用しませんでした"
    return screens, "analysis/screens.json", None


def _build_timeline(
    *,
    session: Mapping[str, object],
    transcript: Sequence[object],
    transcript_path: str,
    transcript_mode: str,
    screens: Sequence[object],
    screen_path: str | None,
    warnings: Sequence[str],
) -> dict[str, object]:
    items: list[dict[str, object]] = []
    for index, raw in enumerate(transcript, 1):
        if not isinstance(raw, dict):
            raise MinutesError(f"文字起こしsegment {index}が不正です")
        start = _finite_non_negative(raw.get("start"), f"segment {index} start")
        end = _finite_non_negative(raw.get("end"), f"segment {index} end")
        text = raw.get("text")
        if end < start or not isinstance(text, str):
            raise MinutesError(f"文字起こしsegment {index}が不正です")
        source = raw.get("source") if isinstance(raw.get("source"), str) else "unknown"
        if transcript_mode == "diarized":
            speaker_name = raw.get("speaker_name")
            speaker_id = raw.get("speaker_id")
        else:
            speaker_name = "自分" if source == "microphone" else "PC音声"
            speaker_id = "self" if source == "microphone" else "system_audio"
        items.append(
            {
                "id": f"speech-{index:05d}",
                "kind": "speech",
                "timestamp_ms": round(start * 1000),
                "start": round(start, 3),
                "end": round(end, 3),
                "source": source,
                "speaker_id": speaker_id if isinstance(speaker_id, str) else "unknown",
                "speaker_name": speaker_name if isinstance(speaker_name, str) else "不明",
                "ambiguous": bool(raw.get("ambiguous", False)),
                "text": text.strip(),
            }
        )
    for index, raw in enumerate(screens, 1):
        if not isinstance(raw, dict) or raw.get("status") != "SUCCEEDED":
            continue
        timestamp_ms = raw.get("timestamp_ms")
        if isinstance(timestamp_ms, bool) or not isinstance(timestamp_ms, int) or timestamp_ms < 0:
            continue
        items.append(
            {
                "id": f"screen-{index:05d}",
                "kind": "screen",
                "timestamp_ms": timestamp_ms,
                "image": raw.get("image") if isinstance(raw.get("image"), str) else "",
                "screen_type": raw.get("type") if isinstance(raw.get("type"), str) else "unknown",
                "title": raw.get("title") if isinstance(raw.get("title"), str) else "",
                "summary": raw.get("summary") if isinstance(raw.get("summary"), str) else "",
                "important": [
                    value for value in raw.get("important", []) if isinstance(value, str)
                ]
                if isinstance(raw.get("important"), list)
                else [],
            }
        )
    items.sort(key=lambda item: (int(item["timestamp_ms"]), str(item["kind"]), str(item["id"])))
    return {
        "schema_version": 1,
        "generated_at": _now_iso(),
        "session": {
            "title": session.get("title") if isinstance(session.get("title"), str) else "会議",
            "started_at": session.get("started_at"),
            "ended_at": session.get("ended_at"),
        },
        "sources": {"transcript": transcript_path, "screens": screen_path},
        "statistics": {
            "speech_count": sum(item["kind"] == "speech" for item in items),
            "screen_count": sum(item["kind"] == "screen" for item in items),
        },
        "items": items,
        "warnings": list(warnings),
    }


def _timeline_chunks(items: object, max_characters: int) -> list[list[object]]:
    if not isinstance(items, list) or not items:
        raise MinutesError("要約できる会話のタイムライン項目がありません")
    chunks: list[list[object]] = []
    current: list[object] = []
    current_size = 0
    for item in items:
        size = len(json.dumps(item, ensure_ascii=False)) + 1
        if current and current_size + size > max_characters:
            chunks.append(current)
            current = []
            current_size = 0
        current.append(item)
        current_size += size
    if current:
        chunks.append(current)
    return chunks


def _generation_prompt(
    session: Mapping[str, object],
    items: Sequence[object],
    *,
    index: int,
    total: int,
) -> str:
    title = session.get("title") if isinstance(session.get("title"), str) else "会議"
    return (
        "あなたは日本語の会話要約者です。会議、雑談、相談、インタビューなどの種類を決めつけず、"
        "以下のタイムラインだけを根拠にJSONを生成してください。\n"
        "全体像が分かるsummary、主なtopics、重要なkey_pointsを中心に整理してください。"
        "推測や一般知識を足してはいけません。各項目には必ず根拠のidをevidence_idsへ入れてください。\n"
        "decisions、todos、pendingは、明示的な合意、今後の対応、未解決事項がある場合だけ記載し、"
        "該当しなければ空配列にしてください。\n"
        "相対的な期限は原文のまま記載し、絶対日付へ変換してはいけません。"
        "担当者または期限が明示されていないTODOは「不明」としてください。\n"
        "画面OCRだけから合意や決定を推測しないでください。\n"
        f"記録名: {title}\n分割: {index}/{total}\nタイムライン:\n"
        + json.dumps(items, ensure_ascii=False, separators=(",", ":"))
    )


def _merge_prompt(session: Mapping[str, object], partials: Sequence[Mapping[str, object]]) -> str:
    title = session.get("title") if isinstance(session.get("title"), str) else "会議"
    return (
        "次の分割会話要約を重複なく統合し、同じJSON形式で返してください。"
        "入力にない事実を追加してはいけません。\n"
        "summary、topics、key_pointsを中心に会話全体を理解できる内容へまとめ、"
        "decisions、todos、pendingは明示的な内容だけを保持してください。\n"
        "evidence_idsは保持し、相対期限は原文のままにしてください。"
        "担当者または期限が不明なTODOは「不明」としてください。\n"
        f"記録名: {title}\n分割会話要約:\n"
        + json.dumps(partials, ensure_ascii=False, separators=(",", ":"))
    )


def _validate_generated(
    value: Mapping[str, object],
    evidence: Mapping[str, Mapping[str, object]],
) -> tuple[dict[str, object], list[str]]:
    warnings: list[str] = []
    participants = _unique_strings(
        item.get("speaker_name")
        for item in evidence.values()
        if item.get("kind") == "speech"
    )
    raw_summary = value.get("summary") if isinstance(value.get("summary"), str) else ""
    summary, summary_changed = _sanitize_generated_text(raw_summary)
    if not summary or summary_changed:
        summary = _extractive_summary(evidence)
        warnings.append("要約に不正な生成トークンがあったため根拠発話から再構成しました")

    def validate_list(name: str, text_keys: Sequence[str]) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        raw_list = value.get(name)
        if not isinstance(raw_list, list):
            return result
        for index, raw in enumerate(raw_list, 1):
            if not isinstance(raw, dict):
                continue
            evidence_ids = [
                item for item in _unique_strings(raw.get("evidence_ids")) if item in evidence
            ]
            if not evidence_ids:
                warnings.append(f"{name} {index}は有効な根拠がないため除外しました")
                continue
            if name in {"decisions", "todos", "pending"} and not any(
                _is_claim_evidence(evidence[item], name) for item in evidence_ids
            ):
                warnings.append(
                    f"{name} {index}は発話または重要画面の根拠がないため除外しました"
                )
                continue
            converted: dict[str, object] = {"evidence_ids": evidence_ids}
            for key in text_keys:
                item = raw.get(key)
                text, changed = _sanitize_generated_text(item if isinstance(item, str) else "")
                converted[key] = text
                if changed:
                    warnings.append(f"{name} {index}の不正な生成トークンを除去しました")
            if name == "todos":
                evidence_text = _evidence_text(evidence_ids, evidence)
                assignee = str(converted["assignee"])
                if not assignee or (
                    assignee not in participants and assignee not in evidence_text
                ):
                    assignee = "不明"
                deadline = str(converted["deadline"])
                if not deadline or deadline not in evidence_text:
                    deadline = _extract_deadline(evidence_text) or "不明"
                converted["assignee"] = assignee
                converted["deadline"] = deadline
            if not any(converted[key] for key in text_keys):
                continue
            if name == "references":
                screen_ids = [
                    item for item in evidence_ids if evidence[item].get("kind") == "screen"
                ]
                if not screen_ids:
                    warnings.append(f"references {index}は画面根拠がないため除外しました")
                    continue
                screen = evidence[screen_ids[0]]
                converted["image"] = screen.get("image", "")
                converted["timestamp_ms"] = screen.get("timestamp_ms", 0)
            result.append(converted)
        return result

    topics = validate_list("topics", ("title", "summary"))
    key_points = validate_list("key_points", ("text",))
    if not key_points:
        key_points = [
            {
                "text": str(topic["summary"]),
                "evidence_ids": list(topic["evidence_ids"]),
            }
            for topic in topics
            if topic.get("summary")
        ]
    if not key_points and summary:
        summary_evidence = [
            evidence_id
            for evidence_id, item in evidence.items()
            if item.get("kind") == "speech"
        ][:3]
        key_points = [{"text": summary, "evidence_ids": summary_evidence}]

    return (
        {
            "summary": summary.strip(),
            "participants": participants,
            "topics": topics,
            "key_points": key_points,
            "decisions": validate_list("decisions", ("text",)),
            "todos": validate_list("todos", ("assignee", "task", "deadline")),
            "pending": validate_list("pending", ("text",)),
            "references": validate_list("references", ("text",)),
        },
        warnings,
    )


def _render_markdown(
    session: Mapping[str, object],
    timeline: Mapping[str, object],
    minutes: Mapping[str, object],
    warnings: Sequence[str],
) -> str:
    title = session.get("title") if isinstance(session.get("title"), str) else "会議"
    participants = (
        minutes.get("participants") if isinstance(minutes.get("participants"), list) else []
    )
    lines = [f"# {title}", "", "## 会話情報", ""]
    lines.extend(
        [
            f"- 記録日時: {_meeting_datetime(session)}",
            f"- 長さ: {_meeting_duration(session, timeline)}",
            f"- 話者: {', '.join(str(item) for item in participants) or '不明'}",
            "",
            "## 会話の要約",
            "",
            str(minutes.get("summary") or "記載なし"),
            "",
            "## 主な話題",
            "",
        ]
    )
    topics = minutes.get("topics") if isinstance(minutes.get("topics"), list) else []
    if topics:
        for topic in topics:
            if isinstance(topic, dict):
                lines.extend(
                    [
                        f"### {topic.get('title') or '話題'}",
                        "",
                        str(topic.get("summary") or ""),
                        "",
                    ]
                )
    else:
        lines.extend(["- 話題を特定できませんでした", ""])

    lines.extend(["## 会話の要点", ""])
    _append_bullets(lines, minutes.get("key_points"), "text")

    decisions = minutes.get("decisions") if isinstance(minutes.get("decisions"), list) else []
    if decisions:
        lines.extend(["## 明確な合意・決定", ""])
        _append_bullets(lines, decisions, "text")

    todos = minutes.get("todos") if isinstance(minutes.get("todos"), list) else []
    if todos:
        lines.extend(["## 今後の対応", "", "| 担当 | 内容 | 時期・期限 |", "|---|---|---|"])
        for todo in todos:
            if isinstance(todo, dict):
                lines.append(
                    f"| {_table(todo.get('assignee'))} | {_table(todo.get('task'))} "
                    f"| {_table(todo.get('deadline'))} |"
                )
        lines.append("")

    pending = minutes.get("pending") if isinstance(minutes.get("pending"), list) else []
    if pending:
        lines.extend(["## 未解決・確認事項", ""])
        _append_bullets(lines, pending, "text")

    references = minutes.get("references") if isinstance(minutes.get("references"), list) else []
    if references:
        lines.extend(["## 関連する画面情報", ""])
        for reference in references:
            if not isinstance(reference, dict):
                continue
            timestamp = _format_timestamp_ms(reference.get("timestamp_ms"))
            image = reference.get("image")
            markdown_image = f"../{image}" if isinstance(image, str) and image else ""
            suffix = f" ([画面]({markdown_image}))" if markdown_image else ""
            lines.append(f"- {timestamp} {reference.get('text') or ''}{suffix}")
        lines.append("")
    if warnings:
        lines.extend(["## 要約時の注意", ""])
        lines.extend(f"- {warning}" for warning in warnings)
    return "\n".join(lines).rstrip() + "\n"


def _append_bullets(lines: list[str], value: object, key: str) -> None:
    items = value if isinstance(value, list) else []
    added = False
    for item in items:
        if isinstance(item, dict) and item.get(key):
            lines.append(f"- {item[key]}")
            added = True
    if not added:
        lines.append("- 記載なし")
    lines.append("")


def _meeting_datetime(session: Mapping[str, object]) -> str:
    value = session.get("started_at")
    if not isinstance(value, str):
        return "不明"
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value


def _meeting_duration(session: Mapping[str, object], timeline: Mapping[str, object]) -> str:
    started = session.get("started_at")
    ended = session.get("ended_at")
    seconds: float | None = None
    if isinstance(started, str) and isinstance(ended, str):
        with suppress(ValueError):
            duration = datetime.fromisoformat(ended) - datetime.fromisoformat(started)
            seconds = max(0.0, duration.total_seconds())
    if seconds is None:
        items = timeline.get("items")
        if isinstance(items, list):
            ends = [
                float(item.get("end", 0))
                for item in items
                if isinstance(item, dict)
                and isinstance(item.get("end"), int | float)
            ]
            seconds = max(ends, default=0.0)
    total = round(seconds or 0)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _extract_json_object(text: str) -> Mapping[str, object]:
    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise MinutesError("ローカルLLMのJSON出力を解析できません")


def _sanitize_generated_text(value: str) -> tuple[str, bool]:
    original = value
    marker = value.find("<|")
    if marker >= 0:
        value = value[:marker]
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value, value != original.strip()


def _extractive_summary(evidence: Mapping[str, Mapping[str, object]]) -> str:
    texts = [
        str(item.get("text")).strip()
        for item in evidence.values()
        if item.get("kind") == "speech" and isinstance(item.get("text"), str)
    ]
    return " ".join(texts)[:600] or "記載なし"


def _is_claim_evidence(item: Mapping[str, object], category: str) -> bool:
    if item.get("kind") == "speech":
        return True
    important = item.get("important")
    if item.get("kind") != "screen" or not isinstance(important, list):
        return False
    keywords = {
        "decisions": ("決定", "確定", "承認", "合意"),
        "todos": ("todo", "担当", "期限", "締切"),
        "pending": ("保留", "未決", "課題"),
    }.get(category, ())
    return any(
        isinstance(value, str) and any(keyword in value.casefold() for keyword in keywords)
        for value in important
    )


def _evidence_text(
    evidence_ids: Sequence[str], evidence: Mapping[str, Mapping[str, object]]
) -> str:
    values: list[str] = []
    for evidence_id in evidence_ids:
        item = evidence[evidence_id]
        for key in ("text", "summary"):
            value = item.get(key)
            if isinstance(value, str):
                values.append(value)
        important = item.get("important")
        if isinstance(important, list):
            values.extend(value for value in important if isinstance(value, str))
    return " ".join(values)


def _extract_deadline(value: str) -> str | None:
    patterns = (
        r"(?:来週|今週|再来週)の?[月火水木金土日]曜日",
        r"(?:今日|明日|明後日|今月末|来月末|月末)",
        r"\d{4}[年/-]\d{1,2}[月/-]\d{1,2}日?",
        r"\d{1,2}月\d{1,2}日",
    )
    for pattern in patterns:
        match = re.search(pattern, value)
        if match is not None:
            return match.group(0)
    return None


def _unique_strings(value: object) -> list[str]:
    if isinstance(value, str):
        values = [value]
    else:
        try:
            values = list(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return []
    return list(
        dict.fromkeys(
            item.strip() for item in values if isinstance(item, str) and item.strip()
        )
    )


def _finite_non_negative(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise MinutesError(f"{label}が不正です")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise MinutesError(f"{label}が不正です")
    return result


def _table(value: object) -> str:
    return str(value or "不明").replace("|", "\\|").replace("\n", " ")


def _format_timestamp_ms(value: object) -> str:
    milliseconds = value if isinstance(value, int) and not isinstance(value, bool) else 0
    minutes, remainder = divmod(max(0, milliseconds), 60_000)
    seconds = remainder // 1000
    return f"{minutes:02d}:{seconds:02d}"


def _read_object(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MinutesError(f"{label}を読み込めません: {exc}") from exc
    if not isinstance(value, dict):
        raise MinutesError(f"{label}の形式が不正です")
    return value


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _notify(callback: ProgressCallback | None, percent: int, message: str) -> None:
    if callback is not None:
        callback(min(100, max(0, percent)), message)
