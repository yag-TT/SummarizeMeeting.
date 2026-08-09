from __future__ import annotations

import json
from pathlib import Path

import pytest

from summarize_meeting.processing.minutes import (
    LlamaCppMinutesBackend,
    MinutesError,
    MinutesService,
)


class FakeBackend:
    runtime_name = "fake-runtime"
    model_name = "fake-model"

    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = responses
        self.prompts: list[str] = []
        self.schemas: list[object] = []

    def generate(self, prompt: str, schema: object) -> dict[str, object]:
        self.prompts.append(prompt)
        self.schemas.append(schema)
        assert isinstance(schema, dict)
        return self.responses.pop(0)


class _HttpResponse:
    def __init__(self, value: object) -> None:
        self._body = json.dumps(value, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        pass

    def read(self) -> bytes:
        return self._body


def _generated() -> dict[str, object]:
    return {
        "summary": "テスト結果の共有予定を確認した。",
        "topics": [
            {
                "title": "テスト結果",
                "summary": "来週金曜日までに共有する。",
                "evidence_ids": ["speech-00002"],
            }
        ],
        "key_points": [
            {
                "text": "テスト結果は来週金曜日までに共有される。",
                "evidence_ids": ["speech-00002"],
            }
        ],
        "decisions": [
            {"text": "来週金曜日までに共有する。", "evidence_ids": ["speech-00002"]},
            {"text": "根拠なし", "evidence_ids": ["missing"]},
        ],
        "todos": [
            {
                "assignee": "",
                "task": "テスト結果を共有する",
                "deadline": "来週金曜日",
                "evidence_ids": ["speech-00002"],
            }
        ],
        "pending": [],
        "references": [
            {"text": "期限を表示した画面", "evidence_ids": ["screen-00001"]}
        ],
        "participants": ["LLMが創作した参加者"],
    }


def _session(tmp_path: Path, *, diarized: bool = True, screens: bool = True) -> Path:
    session = tmp_path / "meeting"
    analysis = session / "analysis"
    output = session / "output"
    analysis.mkdir(parents=True)
    output.mkdir()
    (session / "session.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "title": "Phase 5試験",
                "status": "RECORDED",
                "started_at": "2026-08-08T19:00:00+09:00",
                "ended_at": "2026-08-08T19:01:00+09:00",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    raw_segments = [
        {"start": 1.0, "end": 2.0, "source": "microphone", "text": "お願いします。"},
        {
            "start": 10.0,
            "end": 13.0,
            "source": "system",
            "text": "来週の金曜日までに、テスト結果を共有します。",
        },
    ]
    (analysis / "transcription.json").write_text(
        json.dumps({"status": "SUCCEEDED", "segments": raw_segments}, ensure_ascii=False),
        encoding="utf-8",
    )
    if diarized:
        diarized_segments = [
            {
                **raw_segments[0],
                "speaker_id": "self",
                "speaker_name": "自分",
                "ambiguous": False,
            },
            {
                **raw_segments[1],
                "speaker_id": "speaker_01",
                "speaker_name": "田中",
                "ambiguous": False,
            },
        ]
        (analysis / "diarized_transcription.json").write_text(
            json.dumps(
                {"status": "SUCCEEDED", "segments": diarized_segments},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    if screens:
        (analysis / "screens.json").write_text(
            json.dumps(
                {
                    "status": "SUCCEEDED",
                    "screens": [
                        {
                            "status": "SUCCEEDED",
                            "timestamp_ms": 9000,
                            "image": "screenshots/000001.png",
                            "type": "presentation",
                            "title": "試験計画",
                            "summary": "期限: 来週金曜日",
                            "important": ["期限: 来週金曜日"],
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    return session


def test_service_builds_timeline_validates_evidence_and_renders_minutes(tmp_path: Path) -> None:
    session = _session(tmp_path)
    backend = FakeBackend([_generated()])

    output = MinutesService(backend).run(session)

    timeline = json.loads((session / "analysis" / "timeline.json").read_text(encoding="utf-8"))
    minutes = json.loads((session / "analysis" / "minutes.json").read_text(encoding="utf-8"))
    markdown = output.read_text(encoding="utf-8")
    assert timeline["sources"]["transcript"] == "analysis/diarized_transcription.json"
    assert [item["kind"] for item in timeline["items"]] == ["speech", "screen", "speech"]
    assert minutes["schema_version"] == 2
    assert minutes["minutes"]["participants"] == ["自分", "田中"]
    assert minutes["minutes"]["key_points"][0]["text"].startswith("テスト結果")
    assert len(minutes["minutes"]["decisions"]) == 1
    assert minutes["minutes"]["todos"][0]["assignee"] == "不明"
    assert any("根拠がないため除外" in item for item in minutes["warnings"])
    assert "# Phase 5試験" in markdown
    assert "## 会話の要約" in markdown
    assert "## 会話の要点" in markdown
    assert "## 明確な合意・決定" in markdown
    assert "## 今後の対応" in markdown
    assert "| 不明 | テスト結果を共有する | 来週の金曜日 |" in markdown
    assert "[画面](../screenshots/000001.png)" in markdown


def test_service_falls_back_to_raw_transcript_and_allows_missing_screens(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path, diarized=False, screens=False)
    generated = _generated()
    generated["references"] = []
    backend = FakeBackend([generated])

    MinutesService(backend).run(session)

    timeline = json.loads((session / "analysis" / "timeline.json").read_text(encoding="utf-8"))
    assert timeline["sources"]["transcript"] == "analysis/transcription.json"
    assert timeline["statistics"]["screen_count"] == 0
    assert timeline["items"][1]["speaker_name"] == "PC音声"
    assert "音声文字起こしだけ" in timeline["warnings"][0]


def test_service_uses_map_reduce_for_long_timeline(tmp_path: Path) -> None:
    session = _session(tmp_path, diarized=False, screens=False)
    transcription_path = session / "analysis" / "transcription.json"
    value = json.loads(transcription_path.read_text(encoding="utf-8"))
    value["segments"] = [
        {
            "start": index,
            "end": index + 0.5,
            "source": "system",
            "text": "長い発話です。" * 120,
        }
        for index in range(20)
    ]
    transcription_path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    backend = FakeBackend([_generated() for _ in range(10)])

    MinutesService(backend, max_chunk_characters=10_000).run(session)

    assert len(backend.prompts) > 2
    assert "分割会話要約" in backend.prompts[-1]


def test_service_rejects_session_without_successful_transcript(tmp_path: Path) -> None:
    session = _session(tmp_path)
    (session / "analysis" / "transcription.json").unlink()
    (session / "analysis" / "diarized_transcription.json").unlink()

    with pytest.raises(MinutesError, match="文字起こし結果"):
        MinutesService(FakeBackend([_generated()])).run(session)


def test_llama_cpp_backend_uses_configured_structured_output(monkeypatch) -> None:
    requests = []

    def request(request, **_kwargs):
        requests.append(request)
        return _HttpResponse(
            {"choices": [{"message": {"content": json.dumps(_generated())}}]}
        )

    monkeypatch.setattr("summarize_meeting.processing.minutes.urlopen", request)
    backend = LlamaCppMinutesBackend(
        base_url="http://llm.example.test:8081/v1",
        model="existing-model",
    )

    result = backend.generate("会議資料", {"type": "object"})

    payload = json.loads(requests[0].data.decode("utf-8"))
    assert requests[0].full_url == "http://llm.example.test:8081/v1/chat/completions"
    assert payload["model"] == "existing-model"
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["response_format"]["json_schema"]["name"] == "conversation_summary"
    assert "種類を問わない" in payload["messages"][0]["content"]
    assert result["summary"] == "テスト結果の共有予定を確認した。"


def test_llama_cpp_backend_requires_model_selection_when_multiple_are_visible(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "summarize_meeting.processing.minutes.urlopen",
        lambda *_args, **_kwargs: _HttpResponse(
            {"data": [{"id": "model-a"}, {"id": "model-b"}]}
        ),
    )

    with pytest.raises(MinutesError, match="SUMMARIZE_MEETING_LLM_MODEL"):
        LlamaCppMinutesBackend(base_url="https://llm.example.test/v1").generate(
            "会議資料", {"type": "object"}
        )


def test_llama_cpp_backend_accepts_lan_http_endpoint() -> None:
    backend = LlamaCppMinutesBackend(
        base_url="http://llm.example.test:8081/v1", model="model"
    )

    assert backend.model_name == "model"


def test_llama_cpp_backend_rejects_non_http_endpoint() -> None:
    with pytest.raises(ValueError, match=r"HTTP\(S\)"):
        LlamaCppMinutesBackend(base_url="file:///tmp/llm", model="model")


def test_service_sanitizes_reasoning_tokens_and_hallucinated_deadline(tmp_path: Path) -> None:
    session = _session(tmp_path)
    generated = _generated()
    generated["summary"] = "壊れた要約<|channel>thought秘密の思考"
    generated["todos"][0]["deadline"] = "2023-10-27T09:00:00Z"  # type: ignore[index]
    generated["decisions"] = [
        {"text": "画面だけから推測した決定", "evidence_ids": ["screen-00001"]}
    ]

    MinutesService(FakeBackend([generated])).run(session)

    minutes = json.loads((session / "analysis" / "minutes.json").read_text(encoding="utf-8"))
    assert "<|" not in minutes["minutes"]["summary"]
    assert minutes["minutes"]["summary"] == (
        "お願いします。 来週の金曜日までに、テスト結果を共有します。"
    )
    assert minutes["minutes"]["todos"][0]["deadline"] == "来週の金曜日"
    assert minutes["minutes"]["decisions"] == []


def test_service_summarizes_informal_conversation_without_empty_meeting_sections(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path, diarized=False, screens=False)
    (session / "session.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "title": "旅行についての雑談",
                "status": "RECORDED",
                "started_at": "2026-08-08T19:00:00+09:00",
                "ended_at": "2026-08-08T19:01:00+09:00",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (session / "analysis" / "transcription.json").write_text(
        json.dumps(
            {
                "status": "SUCCEEDED",
                "segments": [
                    {
                        "start": 1.0,
                        "end": 4.0,
                        "source": "microphone",
                        "text": "海辺の町へ行って、景色がとてもきれいでした。",
                    },
                    {
                        "start": 5.0,
                        "end": 8.0,
                        "source": "system",
                        "text": "現地ではどんな料理を食べましたか。",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    generated = {
        "summary": "最近訪れた場所と、そこで食べた料理について話した。",
        "topics": [
            {
                "title": "最近の旅行",
                "summary": "海辺の町を訪れ、景色を楽しんだ。",
                "evidence_ids": ["speech-00001"],
            }
        ],
        "key_points": [
            {
                "text": "海辺の景色が印象に残った。",
                "evidence_ids": ["speech-00001"],
            },
            {
                "text": "現地の料理について感想を共有した。",
                "evidence_ids": ["speech-00002"],
            },
        ],
        "decisions": [],
        "todos": [],
        "pending": [],
        "references": [],
        "participants": [],
    }
    backend = FakeBackend([generated])

    output = MinutesService(backend).run(session)

    markdown = output.read_text(encoding="utf-8")
    assert "## 会話情報" in markdown
    assert "## 会話の要約" in markdown
    assert "## 主な話題" in markdown
    assert "## 会話の要点" in markdown
    assert "海辺の景色が印象に残った" in markdown
    assert "## 明確な合意・決定" not in markdown
    assert "## 今後の対応" not in markdown
    assert "## 未解決・確認事項" not in markdown
    assert "会議、雑談、相談、インタビュー" in backend.prompts[0]
    assert "key_points" in backend.schemas[0]["properties"]  # type: ignore[index]


def test_service_rejects_legacy_session_schema(tmp_path: Path) -> None:
    session = _session(tmp_path)
    (session / "session.json").write_text(
        json.dumps({"schema_version": 1, "status": "RECORDED"}),
        encoding="utf-8",
    )

    with pytest.raises(MinutesError, match="現在のデータ形式"):
        MinutesService(FakeBackend([_generated()])).run(session)
