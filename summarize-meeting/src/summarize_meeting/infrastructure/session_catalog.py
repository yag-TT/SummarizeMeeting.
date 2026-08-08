from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SessionSummary:
    path: Path
    title: str
    started_at: str | None
    recording_status: str
    transcription_status: str
    can_transcribe: bool
    diarization_status: str
    can_diarize: bool

    @property
    def display_label(self) -> str:
        timestamp = _format_started_at(self.started_at) or self.path.name
        transcription = {
            "SUCCEEDED": "文字起こし完了",
            "NOT_STARTED": "文字起こし未実行",
            "INCOMPLETE": "文字起こし要再実行",
            "UNKNOWN": "文字起こし状態不明",
            "FAILED": "文字起こし失敗",
            "CANCELED": "文字起こしキャンセル",
            "RUNNING": "文字起こし前回中断",
        }.get(self.transcription_status, self.transcription_status)
        return f"{timestamp} | {self.title} | {transcription}"


class FileSessionCatalog:
    def __init__(self, meetings_directory: Path) -> None:
        self._meetings_directory = meetings_directory

    def scan(self) -> tuple[SessionSummary, ...]:
        if not self._meetings_directory.is_dir():
            return ()
        summaries: list[tuple[float, SessionSummary]] = []
        try:
            directories = tuple(self._meetings_directory.iterdir())
        except OSError:
            return ()
        for directory in directories:
            if not directory.is_dir():
                continue
            metadata = _read_object(directory / "session.json")
            title = _non_empty_string(metadata.get("title")) or directory.name
            started_at = _non_empty_string(metadata.get("started_at"))
            recording_status = _non_empty_string(metadata.get("status")) or "UNKNOWN"
            transcription_status = _transcription_status(directory)
            diarization_status = _analysis_status(
                directory,
                job="diarization",
                result_name="diarization.json",
            )
            audio_directory = directory / "audio"
            can_transcribe = (audio_directory / "manifest.json").is_file() and any(
                audio_directory.glob("*.wav")
            )
            can_diarize = (
                recording_status == "RECORDED"
                and transcription_status == "SUCCEEDED"
                and _has_system_audio(directory)
            )
            summary = SessionSummary(
                path=directory.resolve(),
                title=title,
                started_at=started_at,
                recording_status=recording_status,
                transcription_status=transcription_status,
                can_transcribe=can_transcribe,
                diarization_status=diarization_status,
                can_diarize=can_diarize,
            )
            summaries.append((_sort_timestamp(directory, started_at), summary))
        summaries.sort(key=lambda value: (value[0], value[1].path.name), reverse=True)
        return tuple(summary for _timestamp, summary in summaries)


def _transcription_status(directory: Path) -> str:
    path = directory / "analysis" / "transcription.json"
    result_exists = path.is_file()
    if result_exists:
        value = _read_object(path)
        status = _non_empty_string(value.get("status"))
        if status == "SUCCEEDED":
            return (
                "SUCCEEDED" if (directory / "output" / "transcript.md").is_file() else "INCOMPLETE"
            )
        if status is not None:
            return status
    jobs = _read_object(directory / "analysis" / "jobs.json").get("jobs")
    if isinstance(jobs, dict):
        transcription = jobs.get("transcription")
        if isinstance(transcription, dict):
            status = _non_empty_string(transcription.get("status"))
            if status is not None:
                return status
    return "UNKNOWN" if result_exists else "NOT_STARTED"


def _analysis_status(directory: Path, *, job: str, result_name: str) -> str:
    result = _read_object(directory / "analysis" / result_name)
    result_exists = (directory / "analysis" / result_name).is_file()
    status = _non_empty_string(result.get("status"))
    if status is not None:
        return status
    jobs = _read_object(directory / "analysis" / "jobs.json").get("jobs")
    if isinstance(jobs, dict):
        state = jobs.get(job)
        if isinstance(state, dict):
            status = _non_empty_string(state.get("status"))
            if status is not None:
                return status
    return "UNKNOWN" if result_exists else "NOT_STARTED"


def _has_system_audio(directory: Path) -> bool:
    manifest = _read_object(directory / "audio" / "manifest.json")
    tracks = manifest.get("tracks")
    if not isinstance(tracks, dict):
        return False
    track = tracks.get("system_audio") or tracks.get("system")
    if not isinstance(track, dict):
        return False
    value = track.get("file")
    if not isinstance(value, str) or not value:
        return False
    relative = Path(value)
    if relative.is_absolute():
        return False
    if relative.parts == (relative.name,):
        audio_path = directory / "audio" / relative
    elif relative.parts == ("audio", relative.name):
        audio_path = directory / relative
    else:
        return False
    return audio_path.is_file()


def _read_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _non_empty_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _sort_timestamp(directory: Path, started_at: str | None) -> float:
    if started_at is not None:
        try:
            return datetime.fromisoformat(started_at).timestamp()
        except ValueError:
            pass
    try:
        return directory.stat().st_mtime
    except OSError:
        return 0.0


def _format_started_at(started_at: str | None) -> str | None:
    if started_at is None:
        return None
    try:
        return datetime.fromisoformat(started_at).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return started_at
