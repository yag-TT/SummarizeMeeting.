"""PC音声から話者区間を推定し、文字起こしへ話者名を割り当てる。"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import wave
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

import av
import numpy as np

from summarize_meeting.domain.diarization import BackendSpeakerTurn, SpeakerTurn
from summarize_meeting.domain.session import (
    AUDIO_MANIFEST_SCHEMA_VERSION,
    SESSION_SCHEMA_VERSION,
)
from summarize_meeting.processing.sherpa_runtime import sherpa_cuda_status

ProgressCallback = Callable[[int, str], None]
BackendProgressCallback = Callable[[float], None]
_ONNXRUNTIME_LIBRARY: object | None = None


class DiarizationError(RuntimeError):
    pass


class DiarizationBackend(Protocol):
    @property
    def runtime_name(self) -> str: ...

    @property
    def segmentation_model_name(self) -> str: ...

    @property
    def embedding_model_name(self) -> str: ...

    @property
    def provider(self) -> str: ...

    @property
    def warnings(self) -> Sequence[str]: ...

    def diarize(
        self,
        audio_path: Path,
        *,
        speaker_count: int | None,
        cluster_threshold: float,
        progress_callback: BackendProgressCallback | None = None,
    ) -> Sequence[BackendSpeakerTurn]: ...


class SherpaOnnxDiarizationBackend:
    def __init__(
        self,
        *,
        segmentation_model: Path,
        cuda_segmentation_model: Path | None = None,
        embedding_model: Path,
        num_threads: int = 4,
    ) -> None:
        if num_threads <= 0:
            raise ValueError("num_threads must be positive")
        self._segmentation_model = segmentation_model.resolve()
        self._cuda_segmentation_model = (
            cuda_segmentation_model.resolve()
            if cuda_segmentation_model is not None
            else self._segmentation_model.with_name("model.onnx")
        )
        self._used_segmentation_model = self._segmentation_model
        self._embedding_model = embedding_model.resolve()
        self._num_threads = num_threads
        self._provider = "cpu"
        self._warnings: tuple[str, ...] = ()

    @property
    def runtime_name(self) -> str:
        return "sherpa-onnx"

    @property
    def segmentation_model_name(self) -> str:
        return self._used_segmentation_model.name

    @property
    def embedding_model_name(self) -> str:
        return self._embedding_model.name

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def warnings(self) -> tuple[str, ...]:
        return self._warnings

    def diarize(
        self,
        audio_path: Path,
        *,
        speaker_count: int | None,
        cluster_threshold: float,
        progress_callback: BackendProgressCallback | None = None,
    ) -> Sequence[BackendSpeakerTurn]:
        self._validate_model_files()
        try:
            sherpa_onnx = _load_sherpa_onnx()
        except ImportError as exc:  # pragma: no cover - dependency is required at runtime
            raise DiarizationError("sherpa-onnxを読み込めません") from exc

        samples = _decode_mono_16k(audio_path)
        provider = self._preferred_provider()
        try:
            turns = self._run_with_provider(
                sherpa_onnx,
                samples,
                provider=provider,
                speaker_count=speaker_count,
                cluster_threshold=cluster_threshold,
                progress_callback=progress_callback,
            )
        except Exception as exc:
            if provider != "cuda" or not _is_cuda_runtime_error(exc):
                if isinstance(exc, DiarizationError):
                    raise
                raise DiarizationError(f"話者分離推論に失敗しました: {exc}") from exc
            self._warnings = (
                "CUDA話者分離に失敗したためCPUへフォールバックしました: " + str(exc),
            )
            try:
                turns = self._run_with_provider(
                    sherpa_onnx,
                    samples,
                    provider="cpu",
                    speaker_count=speaker_count,
                    cluster_threshold=cluster_threshold,
                    progress_callback=progress_callback,
                )
            except Exception as cpu_exc:
                raise DiarizationError(
                    "CUDA話者分離後のCPUフォールバックにも失敗しました: " + str(cpu_exc)
                ) from cpu_exc
            provider = "cpu"
        self._provider = provider
        return turns

    def probe_runtime(self) -> str:
        """Initialize the preferred provider without processing audio."""
        self._validate_model_files()
        try:
            sherpa_onnx = _load_sherpa_onnx()
        except ImportError as exc:  # pragma: no cover - dependency is required at runtime
            raise DiarizationError("sherpa-onnxを読み込めません") from exc
        provider = self._preferred_provider()
        try:
            self._create_diarizer(
                sherpa_onnx,
                provider=provider,
                speaker_count=None,
                cluster_threshold=0.75,
            )
        except Exception as exc:
            if provider != "cuda" or not _is_cuda_runtime_error(exc):
                if isinstance(exc, DiarizationError):
                    raise
                raise DiarizationError(f"話者分離モデルを初期化できません: {exc}") from exc
            self._warnings = (
                "CUDA話者分離に失敗したためCPUへフォールバックしました: " + str(exc),
            )
            try:
                self._create_diarizer(
                    sherpa_onnx,
                    provider="cpu",
                    speaker_count=None,
                    cluster_threshold=0.75,
                )
            except Exception as cpu_exc:
                raise DiarizationError(
                    "CUDA初期化後のCPUフォールバックにも失敗しました: " + str(cpu_exc)
                ) from cpu_exc
            provider = "cpu"
        self._provider = provider
        return provider

    def _validate_model_files(self) -> None:
        if not self._segmentation_model.is_file():
            raise DiarizationError(
                f"話者分離segmentation modelがありません: {self._segmentation_model}"
            )
        if not self._embedding_model.is_file():
            raise DiarizationError(f"話者分離embedding modelがありません: {self._embedding_model}")

    def _preferred_provider(self) -> str:
        status = sherpa_cuda_status()
        if status.available:
            if not self._cuda_segmentation_model.is_file():
                self._warnings = (
                    "CUDA用話者分離モデルがないためCPUへフォールバックしました: "
                    + str(self._cuda_segmentation_model),
                )
                return "cpu"
            self._warnings = ()
            return "cuda"
        self._warnings = (
            ("CUDAを利用できないためCPUへフォールバックしました: " + status.reason,)
            if status.targeted
            else ()
        )
        return "cpu"

    def _create_diarizer(
        self,
        sherpa_onnx,
        *,
        provider: str,
        speaker_count: int | None,
        cluster_threshold: float,
    ):
        segmentation_model = (
            self._cuda_segmentation_model if provider == "cuda" else self._segmentation_model
        )
        self._used_segmentation_model = segmentation_model
        config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
            segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                    model=str(segmentation_model)
                ),
                num_threads=self._num_threads,
                provider=provider,
            ),
            embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=str(self._embedding_model),
                num_threads=self._num_threads,
                provider=provider,
            ),
            clustering=sherpa_onnx.FastClusteringConfig(
                num_clusters=speaker_count if speaker_count is not None else -1,
                threshold=cluster_threshold,
            ),
            min_duration_on=0.3,
            min_duration_off=0.5,
        )
        if not config.validate():
            raise DiarizationError("話者分離モデル設定が不正です")
        return sherpa_onnx.OfflineSpeakerDiarization(config)

    def _run_with_provider(
        self,
        sherpa_onnx,
        samples: np.ndarray,
        *,
        provider: str,
        speaker_count: int | None,
        cluster_threshold: float,
        progress_callback: BackendProgressCallback | None,
    ) -> tuple[BackendSpeakerTurn, ...]:
        diarizer = self._create_diarizer(
            sherpa_onnx,
            provider=provider,
            speaker_count=speaker_count,
            cluster_threshold=cluster_threshold,
        )

        def on_progress(processed: int, total: int) -> int:
            if progress_callback is not None and total > 0:
                progress_callback(min(1.0, max(0.0, processed / total)))
            return 0

        result = diarizer.process(samples, callback=on_progress).sort_by_start_time()
        return tuple(
            BackendSpeakerTurn(
                start=float(turn.start),
                end=float(turn.end),
                speaker=int(turn.speaker),
            )
            for turn in result
        )


class DiarizationService:
    """話者区間の推論、時刻補正、文字起こし統合、結果保存を行う。"""

    def __init__(
        self,
        backend: DiarizationBackend,
        *,
        cluster_threshold: float = 0.75,
        nearest_tolerance_seconds: float = 0.75,
    ) -> None:
        if not 0 < cluster_threshold <= 1:
            raise ValueError("cluster_threshold must be in (0, 1]")
        if nearest_tolerance_seconds < 0:
            raise ValueError("nearest_tolerance_seconds must not be negative")
        self._backend = backend
        self._cluster_threshold = cluster_threshold
        self._nearest_tolerance_seconds = nearest_tolerance_seconds

    def run(
        self,
        session_directory: Path,
        *,
        speaker_count: int | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> Path:
        if speaker_count is not None and not 1 <= speaker_count <= 10:
            raise DiarizationError("話者数は1から10の範囲で指定してください")
        session_directory = session_directory.resolve()
        session_value = _read_object(session_directory / "session.json", "session.json")
        if session_value.get("schema_version") != SESSION_SCHEMA_VERSION:
            raise DiarizationError("現在のデータ形式ではないため話者分離できません")
        if session_value.get("status") != "RECORDED":
            raise DiarizationError("録音完了セッションだけを話者分離できます")
        manifest = _read_object(
            session_directory / "audio" / "manifest.json",
            "音声manifest",
        )
        if manifest.get("schema_version") != AUDIO_MANIFEST_SCHEMA_VERSION:
            raise DiarizationError("現在の音声manifest形式ではありません")
        track = _system_track(manifest)
        audio_path = _resolve_audio_path(session_directory, track.get("file"))
        if not audio_path.is_file():
            raise DiarizationError(f"PC音声ファイルが見つかりません: {audio_path.name}")
        start_offset_ms = _non_negative_int(track.get("estimated_start_offset_ms", 0))
        transcription = _read_object(
            session_directory / "analysis" / "transcription.json",
            "文字起こし結果",
        )
        if transcription.get("status") != "SUCCEEDED":
            raise DiarizationError("成功済みの文字起こし結果が必要です")
        raw_segments = transcription.get("segments")
        if not isinstance(raw_segments, list):
            raise DiarizationError("文字起こしsegmentが不正です")
        if not any(
            isinstance(value, dict) and value.get("source") == "system_audio"
            for value in raw_segments
        ):
            raise DiarizationError("PC音声の文字起こしsegmentがありません")

        _notify(progress_callback, 0, "話者分離モデルとPC音声を準備しています")

        def on_backend_progress(ratio: float) -> None:
            _notify(
                progress_callback,
                5 + round(min(1.0, max(0.0, ratio)) * 80),
                "PC音声から話者を分離しています",
            )

        raw_turns = self._backend.diarize(
            audio_path,
            speaker_count=speaker_count,
            cluster_threshold=self._cluster_threshold,
            progress_callback=on_backend_progress,
        )
        if not raw_turns:
            raise DiarizationError("話者を検出できませんでした")
        # 音声トラック開始offsetを加え、文字起こしと同じ会議時刻へ合わせる。
        turns = _normalize_turns(raw_turns, start_offset_ms=start_offset_ms)
        speaker_ids = tuple(dict.fromkeys(turn.speaker_id for turn in turns))
        warnings = list(getattr(self._backend, "warnings", ()))
        if speaker_count is not None and len(speaker_ids) != speaker_count:
            warnings.append(
                f"指定話者数は{speaker_count}人ですが、{len(speaker_ids)}人を検出しました"
            )
        names = {speaker_id: f"Speaker {index}" for index, speaker_id in enumerate(speaker_ids, 1)}
        _notify(progress_callback, 88, "話者と文字起こしを統合しています")
        # 区間が重なる話者を優先し、重なりがなければ許容範囲内の最近傍を割り当てる。
        merged = merge_transcript_segments(
            raw_segments,
            turns,
            names,
            nearest_tolerance_seconds=self._nearest_tolerance_seconds,
        )
        completed_at = _now_iso()
        diarization_value = {
            "schema_version": 1,
            "status": "SUCCEEDED",
            "completed_at": completed_at,
            "runtime": self._backend.runtime_name,
            "segmentation_model": self._backend.segmentation_model_name,
            "embedding_model": self._backend.embedding_model_name,
            "provider": getattr(self._backend, "provider", "cpu"),
            "source": {
                "file": audio_path.name,
                "start_offset_ms": start_offset_ms,
                "duration_seconds": _wave_duration(audio_path),
            },
            "config": {
                "speaker_count": speaker_count if speaker_count is not None else "auto",
                "cluster_threshold": self._cluster_threshold,
                "min_duration_on": 0.3,
                "min_duration_off": 0.5,
            },
            "speakers": [
                {
                    "id": speaker_id,
                    "default_name": names[speaker_id],
                    "turn_count": sum(turn.speaker_id == speaker_id for turn in turns),
                }
                for speaker_id in speaker_ids
            ],
            "turns": [turn.to_dict() for turn in turns],
            "warnings": warnings,
        }
        names_value = {
            "schema_version": 1,
            "updated_at": completed_at,
            "names": names,
        }
        merged_value = {
            "schema_version": 1,
            "status": "SUCCEEDED",
            "source_transcription_schema_version": transcription.get("schema_version", 1),
            "segments": merged,
        }
        _notify(progress_callback, 94, "話者分離結果を保存しています")
        analysis = session_directory / "analysis"
        output = session_directory / "output"
        _write_json_atomic(analysis / "diarization.json", diarization_value)
        _write_json_atomic(analysis / "speaker_names.json", names_value)
        _write_json_atomic(analysis / "diarized_transcription.json", merged_value)
        transcript_path = output / "transcript.md"
        _write_text_atomic(transcript_path, _render_markdown(merged))
        _notify(progress_callback, 100, "話者分離が完了しました")
        return transcript_path

    def update_speaker_names(
        self,
        session_directory: Path,
        names: Mapping[str, str],
    ) -> Path:
        session_directory = session_directory.resolve()
        session = _read_object(session_directory / "session.json", "セッション情報")
        if session.get("schema_version") != SESSION_SCHEMA_VERSION:
            raise DiarizationError(
                "現在のデータ形式ではないため話者名を更新できません"
            )
        diarization = _read_object(
            session_directory / "analysis" / "diarization.json",
            "話者分離結果",
        )
        raw_speakers = diarization.get("speakers")
        raw_turns = diarization.get("turns")
        if not isinstance(raw_speakers, list) or not isinstance(raw_turns, list):
            raise DiarizationError("話者分離結果が不正です")
        speaker_ids = [
            value.get("id")
            for value in raw_speakers
            if isinstance(value, dict) and isinstance(value.get("id"), str)
        ]
        normalized_names = {
            speaker_id: _validate_speaker_name(
                names.get(speaker_id, ""),
                default=f"Speaker {index}",
            )
            for index, speaker_id in enumerate(speaker_ids, 1)
        }
        turns = tuple(_speaker_turn_from_dict(value) for value in raw_turns)
        transcription = _read_object(
            session_directory / "analysis" / "transcription.json",
            "文字起こし結果",
        )
        raw_segments = transcription.get("segments")
        if not isinstance(raw_segments, list):
            raise DiarizationError("文字起こしsegmentが不正です")
        merged = merge_transcript_segments(
            raw_segments,
            turns,
            normalized_names,
            nearest_tolerance_seconds=self._nearest_tolerance_seconds,
        )
        updated_at = _now_iso()
        _write_json_atomic(
            session_directory / "analysis" / "speaker_names.json",
            {"schema_version": 1, "updated_at": updated_at, "names": normalized_names},
        )
        _write_json_atomic(
            session_directory / "analysis" / "diarized_transcription.json",
            {
                "schema_version": 1,
                "status": "SUCCEEDED",
                "source_transcription_schema_version": transcription.get("schema_version", 1),
                "segments": merged,
            },
        )
        transcript_path = session_directory / "output" / "transcript.md"
        _write_text_atomic(transcript_path, _render_markdown(merged))
        return transcript_path


def merge_transcript_segments(
    raw_segments: Sequence[object],
    turns: Sequence[SpeakerTurn],
    names: Mapping[str, str],
    *,
    nearest_tolerance_seconds: float,
) -> list[dict[str, object]]:
    """文字起こしsegmentへ話者を付与し、マイク発話は「自分」として保持する。"""

    merged: list[dict[str, object]] = []
    for index, value in enumerate(raw_segments):
        if not isinstance(value, dict):
            raise DiarizationError(f"文字起こしsegment {index}が不正です")
        start = _finite_number(value.get("start"), f"segment {index} start")
        end = _finite_number(value.get("end"), f"segment {index} end")
        if start < 0 or end < start:
            raise DiarizationError(f"文字起こしsegment {index}の時刻が不正です")
        source = value.get("source")
        text = value.get("text")
        if not isinstance(source, str) or not isinstance(text, str):
            raise DiarizationError(f"文字起こしsegment {index}の内容が不正です")
        if source == "microphone":
            merged.append(
                {
                    "start": start,
                    "end": end,
                    "source": source,
                    "speaker_id": "self",
                    "speaker_name": "自分",
                    "assignment": "microphone",
                    "ambiguous": False,
                    "overlap_candidates": [],
                    "text": text,
                }
            )
            continue
        if source != "system_audio":
            continue
        assignment = _assign_speaker(
            start,
            end,
            turns,
            nearest_tolerance_seconds=nearest_tolerance_seconds,
        )
        speaker_id = assignment["speaker_id"]
        merged.append(
            {
                "start": start,
                "end": end,
                "source": source,
                "speaker_id": speaker_id,
                "speaker_name": names.get(str(speaker_id), "不明"),
                "assignment": assignment["assignment"],
                "ambiguous": assignment["ambiguous"],
                "overlap_candidates": assignment["overlap_candidates"],
                "text": text,
            }
        )
    merged.sort(key=lambda item: (float(item["start"]), float(item["end"]), str(item["source"])))
    return merged


def _assign_speaker(
    start: float,
    end: float,
    turns: Sequence[SpeakerTurn],
    *,
    nearest_tolerance_seconds: float,
) -> dict[str, object]:
    overlap_by_speaker: defaultdict[str, float] = defaultdict(float)
    for turn in turns:
        overlap = max(0.0, min(end, turn.end) - max(start, turn.start))
        overlap_by_speaker[turn.speaker_id] += overlap
    candidates = sorted(overlap_by_speaker.items(), key=lambda item: (-item[1], item[0]))
    positive = [(speaker, seconds) for speaker, seconds in candidates if seconds > 0]
    if positive:
        speaker_id, primary_seconds = positive[0]
        duration = max(0.001, end - start)
        second_seconds = positive[1][1] if len(positive) > 1 else 0.0
        return {
            "speaker_id": speaker_id,
            "assignment": "dominant_overlap",
            "ambiguous": primary_seconds / duration < 0.5 or second_seconds / duration >= 0.25,
            "overlap_candidates": [
                {"speaker_id": speaker, "seconds": round(seconds, 3)}
                for speaker, seconds in positive
            ],
        }
    nearest: tuple[float, SpeakerTurn] | None = None
    for turn in turns:
        distance = min(abs(start - turn.end), abs(end - turn.start))
        candidate = (distance, turn)
        if nearest is None or (candidate[0], candidate[1].speaker_id) < (
            nearest[0],
            nearest[1].speaker_id,
        ):
            nearest = candidate
    if nearest is not None and nearest[0] <= nearest_tolerance_seconds:
        return {
            "speaker_id": nearest[1].speaker_id,
            "assignment": "nearest_turn",
            "ambiguous": True,
            "overlap_candidates": [],
        }
    return {
        "speaker_id": "unknown",
        "assignment": "unknown",
        "ambiguous": True,
        "overlap_candidates": [],
    }


def _normalize_turns(
    raw_turns: Iterable[BackendSpeakerTurn],
    *,
    start_offset_ms: int,
) -> tuple[SpeakerTurn, ...]:
    values = sorted(raw_turns, key=lambda turn: (turn.start, turn.end, turn.speaker))
    if any(
        not math.isfinite(turn.start)
        or not math.isfinite(turn.end)
        or turn.start < 0
        or turn.end < turn.start
        for turn in values
    ):
        raise DiarizationError("話者turnの時刻が不正です")
    speaker_order: dict[int, str] = {}
    offset = start_offset_ms / 1000
    normalized: list[SpeakerTurn] = []
    for turn in values:
        speaker_id = speaker_order.setdefault(
            turn.speaker,
            f"speaker_{len(speaker_order) + 1:02d}",
        )
        normalized.append(
            SpeakerTurn(
                start=round(offset + turn.start, 3),
                end=round(offset + turn.end, 3),
                audio_start=round(turn.start, 3),
                audio_end=round(turn.end, 3),
                speaker_id=speaker_id,
            )
        )
    return tuple(normalized)


def _decode_mono_16k(path: Path) -> np.ndarray:
    try:
        container = av.open(str(path))
    except Exception as exc:
        raise DiarizationError(f"PC音声を開けません: {exc}") from exc
    chunks: list[np.ndarray] = []
    resampler = av.AudioResampler(format="fltp", layout="mono", rate=16_000)
    try:
        for frame in container.decode(audio=0):
            for converted in resampler.resample(frame):
                chunks.append(np.asarray(converted.to_ndarray(), dtype=np.float32).reshape(-1))
        for converted in resampler.resample(None):
            chunks.append(np.asarray(converted.to_ndarray(), dtype=np.float32).reshape(-1))
    except Exception as exc:
        raise DiarizationError(f"PC音声を16 kHz monoへ変換できません: {exc}") from exc
    finally:
        container.close()
    if not chunks:
        raise DiarizationError("PC音声が空です")
    samples = np.concatenate(chunks)
    if not np.any(np.abs(samples) >= 0.001):
        raise DiarizationError("PC音声が無音です")
    return np.ascontiguousarray(samples, dtype=np.float32)


def _load_sherpa_onnx():
    global _ONNXRUNTIME_LIBRARY
    if sys.platform == "win32" and _ONNXRUNTIME_LIBRARY is None:
        import ctypes

        import onnxruntime

        runtime_path = Path(onnxruntime.__file__).resolve().parent / "capi" / "onnxruntime.dll"
        if not runtime_path.is_file():
            raise DiarizationError(f"ONNX Runtime DLLがありません: {runtime_path}")
        try:
            _ONNXRUNTIME_LIBRARY = ctypes.WinDLL(str(runtime_path))
        except OSError as exc:
            raise DiarizationError(f"ONNX Runtime DLLを読み込めません: {exc}") from exc
    import sherpa_onnx

    return sherpa_onnx


def _is_cuda_runtime_error(error: BaseException) -> bool:
    cuda_markers = (
        "cuda",
        "cudnn",
        "cublas",
        "cufft",
        "curand",
        "cudart",
        "gpu",
        "out of memory",
        "onnxruntime_providers_cuda",
    )
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        message = str(current).casefold()
        if any(marker in message for marker in cuda_markers):
            return True
        current = current.__cause__ or current.__context__
    return False


def _system_track(manifest: dict[str, object]) -> dict[str, object]:
    tracks = manifest.get("tracks")
    if not isinstance(tracks, dict):
        raise DiarizationError("音声manifestにtracksがありません")
    value = tracks.get("system_audio")
    if isinstance(value, dict):
        return value
    raise DiarizationError("PC音声trackがありません")


def _resolve_audio_path(session: Path, value: object) -> Path:
    if not isinstance(value, str) or not value:
        raise DiarizationError("PC音声ファイル名が不正です")
    relative = Path(value)
    if relative.is_absolute():
        raise DiarizationError("PC音声ファイル名が不正です")
    if relative.parts != (relative.name,):
        raise DiarizationError("PC音声ファイル名が不正です")
    return session / "audio" / relative


def _speaker_turn_from_dict(value: object) -> SpeakerTurn:
    if not isinstance(value, dict):
        raise DiarizationError("話者turnが不正です")
    speaker_id = value.get("speaker_id")
    if not isinstance(speaker_id, str):
        raise DiarizationError("話者IDが不正です")
    return SpeakerTurn(
        start=_finite_number(value.get("start"), "turn start"),
        end=_finite_number(value.get("end"), "turn end"),
        audio_start=_finite_number(value.get("audio_start"), "turn audio_start"),
        audio_end=_finite_number(value.get("audio_end"), "turn audio_end"),
        speaker_id=speaker_id,
    )


def _validate_speaker_name(value: str, *, default: str) -> str:
    normalized = value.strip()
    if not normalized:
        return default
    if len(normalized) > 80:
        raise DiarizationError("話者名は80文字以内で入力してください")
    if re.search(r"[\x00-\x1f\x7f]", normalized):
        raise DiarizationError("話者名に改行または制御文字を使用できません")
    return normalized


def _render_markdown(segments: Sequence[Mapping[str, object]]) -> str:
    lines = ["# Transcript", ""]
    for segment in segments:
        lines.extend(
            [
                f"## {_format_timestamp(float(segment['start']))}",
                f"**{segment['speaker_name']}**",
                str(segment["text"]),
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _format_timestamp(value: float) -> str:
    milliseconds = max(0, round(value * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def _wave_duration(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as stream:
            return stream.getnframes() / stream.getframerate()
    except (OSError, wave.Error, ZeroDivisionError) as exc:
        raise DiarizationError(f"PC音声WAVを検証できません: {exc}") from exc


def _read_object(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DiarizationError(f"{label}を読み込めません: {exc}") from exc
    if not isinstance(value, dict):
        raise DiarizationError(f"{label}の形式が不正です")
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


def _write_text_atomic(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _non_negative_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DiarizationError("PC音声の開始offsetが不正です")
    return value


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise DiarizationError(f"{label}が不正です")
    return float(value)


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _notify(callback: ProgressCallback | None, percent: int, message: str) -> None:
    if callback is not None:
        callback(min(100, max(0, percent)), message)
