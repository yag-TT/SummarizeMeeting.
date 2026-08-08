from __future__ import annotations

import json
import os
import struct
import threading
import wave
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal

from summarize_meeting.domain.session import SessionStatus

_INTERRUPTED_STATUSES = {
    SessionStatus.PREPARING.value,
    SessionStatus.RECORDING.value,
    SessionStatus.STOPPING.value,
    SessionStatus.FINALIZING.value,
}


@dataclass(frozen=True, slots=True)
class InterruptedSession:
    root: Path
    session_json: Path
    title: str
    status: str


@dataclass(frozen=True, slots=True)
class RecoveredTrack:
    track: str
    output: str
    frames: int
    sample_rate: int
    channels: int
    recovered_segments: int
    skipped_segments: int


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    session_root: str
    tracks: tuple[RecoveredTrack, ...]
    warnings: tuple[str, ...]


class SessionRecoveryService:
    def __init__(self, meetings_root: Path) -> None:
        self._meetings_root = meetings_root

    def scan(self) -> list[InterruptedSession]:
        if not self._meetings_root.exists():
            return []
        candidates: list[InterruptedSession] = []
        for session_json in self._meetings_root.glob("*/session.json"):
            try:
                value = json.loads(session_json.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            status = str(value.get("status", ""))
            if status not in _INTERRUPTED_STATUSES:
                continue
            candidates.append(
                InterruptedSession(
                    root=session_json.parent,
                    session_json=session_json,
                    title=str(value.get("title") or session_json.parent.name),
                    status=status,
                )
            )
        return sorted(candidates, key=lambda candidate: candidate.root.name)

    def recover(self, candidate: InterruptedSession) -> RecoveryResult:
        warnings: list[str] = []
        recovered_tracks: list[RecoveredTrack] = []
        work_root = candidate.root / "audio" / ".work"
        if work_root.is_dir():
            for track_dir in sorted(path for path in work_root.iterdir() if path.is_dir()):
                result = self._recover_track(candidate.root, track_dir, warnings)
                if result is not None:
                    recovered_tracks.append(result)
        else:
            warnings.append("audio/.work が見つかりません")

        session_value = json.loads(candidate.session_json.read_text(encoding="utf-8"))
        recovered_at = datetime.now().astimezone().isoformat(timespec="seconds")
        session_value["status"] = SessionStatus.INTERRUPTED.value
        session_value["recovery"] = {
            "recovered_at": recovered_at,
            "tracks": [asdict(track) for track in recovered_tracks],
            "warnings": warnings,
        }
        warning_list = session_value.setdefault("warnings", [])
        warning_list.append(
            {
                "code": "SESSION_RECOVERY_COMPLETED"
                if recovered_tracks
                else "SESSION_RECOVERY_NO_AUDIO",
                "message": self._summary_message(recovered_tracks, warnings),
                "timestamp_ms": session_value.get("duration_ms") or 0,
            }
        )
        self._write_json_atomic(candidate.session_json, session_value)
        self._append_event(
            candidate.root / "events.jsonl",
            {
                "schema_version": 1,
                "timestamp_ms": session_value.get("duration_ms") or 0,
                "type": "session_recovered",
                "tracks": [asdict(track) for track in recovered_tracks],
                "warnings": warnings,
            },
        )
        return RecoveryResult(
            session_root=str(candidate.root),
            tracks=tuple(recovered_tracks),
            warnings=tuple(warnings),
        )

    def _recover_track(
        self,
        session_root: Path,
        track_dir: Path,
        warnings: list[str],
    ) -> RecoveredTrack | None:
        sources = sorted(track_dir.glob("*.wav"))
        if not sources:
            warnings.append(f"{track_dir.name}: WAV segmentがありません")
            return None

        recovery_dir = session_root / "audio" / ".recovery" / track_dir.name
        recovery_dir.mkdir(parents=True, exist_ok=True)
        repaired: list[Path] = []
        skipped = 0
        expected_format: tuple[int, int, int] | None = None
        for source in sources:
            target = recovery_dir / source.name
            try:
                audio_format = self._repair_wave_copy(source, target)
                if expected_format is None:
                    expected_format = audio_format
                elif audio_format != expected_format:
                    raise RuntimeError(
                        f"format mismatch expected={expected_format} actual={audio_format}"
                    )
                repaired.append(target)
            except Exception as exc:
                skipped += 1
                warnings.append(f"{track_dir.name}/{source.name}: {exc}")

        if not repaired or expected_format is None:
            return None
        output = session_root / "audio" / f"{track_dir.name}.recovered.wav"
        frames = self._consolidate(repaired, output, expected_format)
        channels, sample_width, sample_rate = expected_format
        return RecoveredTrack(
            track=track_dir.name,
            output=str(output.relative_to(session_root)).replace("\\", "/"),
            frames=frames,
            sample_rate=sample_rate,
            channels=channels,
            recovered_segments=len(repaired),
            skipped_segments=skipped,
        )

    @staticmethod
    def _repair_wave_copy(source: Path, target: Path) -> tuple[int, int, int]:
        content = bytearray(source.read_bytes())
        if len(content) < 44 or content[:4] != b"RIFF" or content[8:12] != b"WAVE":
            raise RuntimeError("PCM RIFF/WAVEではありません")

        fmt_position = content.find(b"fmt ", 12, min(len(content), 4096))
        data_position = content.find(b"data", 12, min(len(content), 4096))
        if fmt_position < 0 or data_position < 0:
            raise RuntimeError("fmt/data chunkが見つかりません")
        fmt_size = struct.unpack_from("<I", content, fmt_position + 4)[0]
        if fmt_size < 16 or fmt_position + 8 + fmt_size > len(content):
            raise RuntimeError("fmt chunkが不正です")
        audio_format, channels, sample_rate, _byte_rate, block_align, bits = struct.unpack_from(
            "<HHIIHH", content, fmt_position + 8
        )
        if audio_format != 1 or channels <= 0 or bits != 16 or block_align != channels * 2:
            raise RuntimeError("対応していないWAV形式です")

        data_start = data_position + 8
        actual_data_size = len(content) - data_start
        actual_data_size -= actual_data_size % block_align
        if actual_data_size < 0:
            raise RuntimeError("data chunkが不正です")
        del content[data_start + actual_data_size :]
        struct.pack_into("<I", content, data_position + 4, actual_data_size)
        struct.pack_into("<I", content, 4, len(content) - 8)
        temporary = target.with_suffix(".wav.tmp")
        temporary.write_bytes(content)
        os.replace(temporary, target)

        with wave.open(str(target), "rb") as stream:
            if stream.getnframes() * block_align != actual_data_size:
                raise RuntimeError("修復後のframe数が一致しません")
        return channels, bits // 8, sample_rate

    @staticmethod
    def _consolidate(
        repaired: Sequence[Path],
        output: Path,
        audio_format: tuple[int, int, int],
    ) -> int:
        channels, sample_width, sample_rate = audio_format
        temporary = output.with_suffix(".wav.tmp")
        total_frames = 0
        with wave.open(str(temporary), "wb") as target:
            target.setnchannels(channels)
            target.setsampwidth(sample_width)
            target.setframerate(sample_rate)
            for path in repaired:
                with wave.open(str(path), "rb") as source:
                    if (
                        source.getnchannels(),
                        source.getsampwidth(),
                        source.getframerate(),
                    ) != audio_format:
                        raise RuntimeError(f"Segment format mismatch: {path}")
                    total_frames += source.getnframes()
                    while frames := source.readframes(65_536):
                        target.writeframesraw(frames)
        os.replace(temporary, output)
        return total_frames

    @staticmethod
    def _write_json_atomic(path: Path, value: Any) -> None:
        temporary = path.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)

    @staticmethod
    def _append_event(path: Path, event: dict[str, Any]) -> None:
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
            stream.write("\n")
            stream.flush()

    @staticmethod
    def _summary_message(
        tracks: Sequence[RecoveredTrack],
        warnings: Sequence[str],
    ) -> str:
        return f"復旧トラック {len(tracks)}件、警告 {len(warnings)}件"


class RecoveryController(QObject):
    progress = Signal(str)
    finished = Signal(str)
    failed = Signal(str)

    def __init__(self, service: SessionRecoveryService) -> None:
        super().__init__()
        self._service = service
        self._thread: threading.Thread | None = None

    def scan(self) -> list[InterruptedSession]:
        return self._service.scan()

    def recover_all(self, candidates: Sequence[InterruptedSession]) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._recover_worker,
            args=(tuple(candidates),),
            name="session-recovery",
            daemon=True,
        )
        self._thread.start()

    def _recover_worker(self, candidates: Sequence[InterruptedSession]) -> None:
        try:
            summaries: list[str] = []
            for index, candidate in enumerate(candidates, start=1):
                self.progress.emit(
                    f"中断セッションを復旧しています ({index}/{len(candidates)}): {candidate.title}"
                )
                result = self._service.recover(candidate)
                summaries.append(
                    f"{candidate.title}: {len(result.tracks)}トラック、警告{len(result.warnings)}件"
                )
            self.finished.emit("復旧が完了しました。" + " / ".join(summaries))
        except Exception as exc:
            self.failed.emit(f"中断セッションの復旧に失敗しました: {exc}")
