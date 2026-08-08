from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from summarize_meeting.domain.transcript import TranscribedTrack, TranscriptSegment

ProgressCallback = Callable[[int, str], None]


class TranscriptionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BackendSegment:
    start: float
    end: float
    text: str
    avg_logprob: float | None = None
    no_speech_prob: float | None = None


@dataclass(frozen=True, slots=True)
class BackendTranscription:
    segments: tuple[BackendSegment, ...]
    detected_language: str
    language_probability: float
    duration_seconds: float
    runtime_device: str = "unknown"


class TranscriptionBackend(Protocol):
    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str,
        progress_callback: Callable[[float], None] | None = None,
    ) -> BackendTranscription: ...


class FasterWhisperBackend:
    def __init__(self, *, model_name: str, models_directory: Path) -> None:
        self._model_name = model_name
        self._models_directory = models_directory
        self._model: Any | None = None
        self._device: str | None = None
        self._force_cpu = False

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str,
        progress_callback: Callable[[float], None] | None = None,
    ) -> BackendTranscription:
        try:
            return self._transcribe_with_model(
                audio_path,
                language=language,
                progress_callback=progress_callback,
            )
        except RuntimeError as exc:
            if self._device != "cuda" or not _is_cuda_runtime_error(exc):
                raise
            logging.getLogger(__name__).warning(
                "CUDA runtime is unavailable; retrying transcription on CPU: %s",
                exc,
            )
            self._model = None
            self._device = None
            self._force_cpu = True
            return self._transcribe_with_model(
                audio_path,
                language=language,
                progress_callback=progress_callback,
            )

    def _transcribe_with_model(
        self,
        audio_path: Path,
        *,
        language: str,
        progress_callback: Callable[[float], None] | None,
    ) -> BackendTranscription:
        model = self._load_model()
        segments, info = model.transcribe(
            str(audio_path),
            language=language,
            task="transcribe",
            beam_size=5,
            vad_filter=True,
            word_timestamps=False,
            condition_on_previous_text=True,
        )
        duration = max(0.0, float(info.duration))
        converted: list[BackendSegment] = []
        for segment in segments:
            text = segment.text.strip()
            if text:
                converted.append(
                    BackendSegment(
                        start=float(segment.start),
                        end=float(segment.end),
                        text=text,
                        avg_logprob=float(segment.avg_logprob),
                        no_speech_prob=float(segment.no_speech_prob),
                    )
                )
            if progress_callback is not None and duration > 0:
                progress_callback(min(1.0, max(0.0, float(segment.end) / duration)))
        if progress_callback is not None:
            progress_callback(1.0)
        return BackendTranscription(
            segments=tuple(converted),
            detected_language=str(info.language),
            language_probability=float(info.language_probability),
            duration_seconds=duration,
            runtime_device=self._device or "unknown",
        )

    def _load_model(self) -> Any:
        if self._model is None:
            import ctranslate2
            from faster_whisper import WhisperModel

            self._models_directory.mkdir(parents=True, exist_ok=True)
            has_cuda = not self._force_cpu and ctranslate2.get_cuda_device_count() > 0
            self._device = "cuda" if has_cuda else "cpu"
            self._model = WhisperModel(
                self._model_name,
                device=self._device,
                compute_type="float16" if has_cuda else "int8",
                download_root=str(self._models_directory),
            )
        return self._model


class TranscriptionService:
    _SOURCE_NAMES = {
        "microphone": "microphone",
        "system": "system",
        "system_audio": "system",
    }

    def __init__(self, backend: TranscriptionBackend, *, model_name: str) -> None:
        self._backend = backend
        self._model_name = model_name

    def run(
        self,
        session_directory: Path,
        *,
        language: str = "ja",
        progress_callback: ProgressCallback | None = None,
    ) -> Path:
        session_directory = session_directory.resolve()
        audio_directory = session_directory / "audio"
        manifest_path = audio_directory / "manifest.json"
        manifest = self._read_object(manifest_path, "音声manifest")
        manifest_tracks = manifest.get("tracks")
        if not isinstance(manifest_tracks, dict):
            raise TranscriptionError("音声manifestにtracksがありません")

        inputs = list(self._iter_track_inputs(audio_directory, manifest_tracks))
        if not inputs:
            raise TranscriptionError("文字起こし可能な音声トラックがありません")

        self._notify(progress_callback, 0, "文字起こしモデルを準備しています")
        all_segments: list[TranscriptSegment] = []
        tracks: list[TranscribedTrack] = []
        track_count = len(inputs)
        for index, (source, audio_path, offset_ms) in enumerate(inputs):
            label = "マイク" if source == "microphone" else "PC音声"
            start_percent = round(index * 90 / track_count)
            end_percent = round((index + 1) * 90 / track_count)
            self._notify(progress_callback, start_percent, f"{label}を文字起こししています")

            def on_track_progress(
                ratio: float,
                start: int = start_percent,
                end: int = end_percent,
                track_label: str = label,
            ) -> None:
                value = start + round((end - start) * ratio)
                self._notify(
                    progress_callback,
                    value,
                    f"{track_label}を文字起こししています",
                )

            result = self._backend.transcribe(
                audio_path,
                language=language,
                progress_callback=on_track_progress,
            )
            offset_seconds = offset_ms / 1000
            converted = [
                TranscriptSegment(
                    start=round(offset_seconds + segment.start, 3),
                    end=round(offset_seconds + segment.end, 3),
                    source=source,
                    text=segment.text,
                    avg_logprob=segment.avg_logprob,
                    no_speech_prob=segment.no_speech_prob,
                )
                for segment in result.segments
            ]
            all_segments.extend(converted)
            tracks.append(
                TranscribedTrack(
                    source=source,
                    file=audio_path.name,
                    start_offset_ms=offset_ms,
                    detected_language=result.detected_language,
                    language_probability=result.language_probability,
                    duration_seconds=result.duration_seconds,
                    segment_count=len(converted),
                    runtime_device=result.runtime_device,
                )
            )

        all_segments.sort(key=lambda value: (value.start, value.end, value.source))
        completed_at = datetime.now().astimezone().isoformat(timespec="milliseconds")
        payload = {
            "schema_version": 1,
            "status": "SUCCEEDED",
            "model": self._model_name,
            "requested_language": language,
            "completed_at": completed_at,
            "tracks": [track.to_dict() for track in tracks],
            "segments": [segment.to_dict() for segment in all_segments],
        }
        analysis_path = session_directory / "analysis" / "transcription.json"
        transcript_path = session_directory / "output" / "transcript.md"
        self._notify(progress_callback, 94, "文字起こし結果を保存しています")
        self._write_json_atomic(analysis_path, payload)
        self._write_text_atomic(transcript_path, self._render_markdown(all_segments))
        self._notify(progress_callback, 100, "文字起こしが完了しました")
        return transcript_path

    def _iter_track_inputs(
        self,
        audio_directory: Path,
        tracks: dict[str, object],
    ) -> Iterable[tuple[str, Path, int]]:
        for manifest_name in ("microphone", "system", "system_audio"):
            value = tracks.get(manifest_name)
            if not isinstance(value, dict):
                continue
            file_value = value.get("file")
            if not isinstance(file_value, str) or not file_value:
                raise TranscriptionError(f"{manifest_name}の音声ファイル名が不正です")
            relative_path = Path(file_value)
            if relative_path.name != file_value:
                raise TranscriptionError(f"{manifest_name}の音声ファイル名が不正です")
            audio_path = audio_directory / relative_path
            if not audio_path.is_file():
                raise TranscriptionError(f"音声ファイルが見つかりません: {audio_path.name}")
            offset_value = value.get("estimated_start_offset_ms", 0)
            if offset_value is None:
                offset_value = 0
            if isinstance(offset_value, bool) or not isinstance(offset_value, int):
                raise TranscriptionError(f"{manifest_name}の開始時刻が不正です")
            yield self._SOURCE_NAMES[manifest_name], audio_path, max(0, offset_value)

    @staticmethod
    def _read_object(path: Path, label: str) -> dict[str, object]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TranscriptionError(f"{label}を読み込めません: {exc}") from exc
        if not isinstance(value, dict):
            raise TranscriptionError(f"{label}の形式が不正です")
        return value

    @staticmethod
    def _render_markdown(segments: list[TranscriptSegment]) -> str:
        lines = ["# Transcript", ""]
        for segment in segments:
            speaker = "自分" if segment.source == "microphone" else "PC音声"
            lines.extend(
                [
                    f"## {_format_timestamp(segment.start)}",
                    f"**{speaker}**",
                    segment.text,
                    "",
                ]
            )
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _write_json_atomic(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)

    @staticmethod
    def _write_text_atomic(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)

    @staticmethod
    def _notify(callback: ProgressCallback | None, percent: int, message: str) -> None:
        if callback is not None:
            callback(min(100, max(0, percent)), message)


def _format_timestamp(value: float) -> str:
    milliseconds = max(0, round(value * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def _is_cuda_runtime_error(error: RuntimeError) -> bool:
    message = str(error).lower()
    return any(
        marker in message
        for marker in (
            "cuda",
            "cublas",
            "cudnn",
            "curand",
            "nvrtc",
        )
    )
