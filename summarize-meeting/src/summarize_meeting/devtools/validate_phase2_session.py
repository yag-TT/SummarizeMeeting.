from __future__ import annotations

import argparse
import json
import math
import wave
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np


@dataclass(slots=True)
class ValidationReport:
    session: str
    passed: bool = True
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metrics: dict[str, object] = field(default_factory=dict)

    def fail(self, message: str) -> None:
        self.passed = False
        self.failures.append(message)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def validate_phase2_session(
    session: Path,
    *,
    expected_microphone_text: str | None = None,
    expected_system_text: str | None = None,
) -> ValidationReport:
    session = session.resolve()
    report = ValidationReport(session=str(session))
    session_value = _read_object(session / "session.json", "session.json", report)
    manifest = _read_object(session / "audio" / "manifest.json", "audio manifest", report)
    transcription = _read_object(
        session / "analysis" / "transcription.json",
        "transcription.json",
        report,
    )
    jobs = _read_object(session / "analysis" / "jobs.json", "jobs.json", report)
    if None in (session_value, manifest, transcription, jobs):
        return report

    if session_value.get("status") != "RECORDED":
        report.fail(f"session status is not RECORDED: {session_value.get('status')}")

    tracks = manifest.get("tracks")
    if not isinstance(tracks, dict):
        report.fail("audio manifest has no tracks object")
        tracks = {}
    audio_metrics: dict[str, object] = {}
    for source, candidates in (
        ("microphone", ("microphone",)),
        ("system", ("system_audio", "system")),
    ):
        name, track = _find_track(tracks, candidates)
        if track is None:
            report.fail(f"{source} audio track is missing")
            continue
        file_value = track.get("file")
        audio_path = _resolve_audio_path(session, file_value)
        if audio_path is None or not audio_path.is_file():
            report.fail(f"{source} WAV path is invalid or missing: {file_value!r}")
            continue
        try:
            wav_metrics = _wave_metrics(audio_path)
        except (OSError, EOFError, wave.Error, ValueError) as exc:
            report.fail(f"{source} WAV cannot be read: {exc}")
            continue
        audio_metrics[source] = {"manifest_track": name, **wav_metrics}
        if wav_metrics["duration_seconds"] <= 0:
            report.fail(f"{source} WAV is empty")
        if wav_metrics["peak"] < 0.001:
            report.fail(f"{source} WAV is effectively silent")
        if track.get("validated") is not True:
            report.fail(f"{source} WAV is not marked validated")
        if track.get("overflow_count") != 0:
            report.fail(f"{source} overflow_count is not zero")
        if track.get("queue_pressure_count") != 0:
            report.fail(f"{source} queue_pressure_count is not zero")
        gaps = track.get("gaps")
        if isinstance(gaps, list) and gaps:
            report.warnings.append(f"{source} contains {len(gaps)} capture gap(s)")
    report.metrics["audio"] = audio_metrics

    if transcription.get("status") != "SUCCEEDED":
        report.fail(f"transcription status is not SUCCEEDED: {transcription.get('status')}")
    transcript_tracks = transcription.get("tracks")
    if not isinstance(transcript_tracks, list):
        report.fail("transcription tracks is not an array")
        transcript_tracks = []
    runtime_devices: dict[str, object] = {}
    for track in transcript_tracks:
        if isinstance(track, dict) and isinstance(track.get("source"), str):
            runtime_devices[track["source"]] = track.get("runtime_device")
    report.metrics["runtime_devices"] = runtime_devices

    segments = transcription.get("segments")
    if not isinstance(segments, list):
        report.fail("transcription segments is not an array")
        segments = []
    source_text: dict[str, list[str]] = {"microphone": [], "system": []}
    for index, segment in enumerate(segments):
        if not isinstance(segment, dict):
            report.fail(f"segment {index} is not an object")
            continue
        start = segment.get("start")
        end = segment.get("end")
        if not _valid_timestamp(start, end):
            report.fail(f"segment {index} has an invalid timestamp")
        source = segment.get("source")
        text = segment.get("text")
        if source in source_text and isinstance(text, str):
            source_text[source].append(text)
    report.metrics["segment_count"] = len(segments)
    for source in ("microphone", "system"):
        if not source_text[source]:
            report.fail(f"transcription has no {source} segment")

    expected = {
        "microphone": expected_microphone_text,
        "system": expected_system_text,
    }
    for source, expected_text in expected.items():
        if expected_text and expected_text not in "".join(source_text[source]):
            report.fail(f"expected {source} text was not recognized: {expected_text}")

    transcript_path = session / "output" / "transcript.md"
    try:
        markdown = transcript_path.read_text(encoding="utf-8")
    except OSError as exc:
        report.fail(f"transcript.md cannot be read: {exc}")
    else:
        markdown_segment_count = sum(line.startswith("## ") for line in markdown.splitlines())
        report.metrics["markdown_segment_count"] = markdown_segment_count
        if markdown_segment_count != len(segments):
            report.fail("Markdown segment count does not match transcription JSON")

    job_values = jobs.get("jobs")
    transcription_job = job_values.get("transcription") if isinstance(job_values, dict) else None
    if not isinstance(transcription_job, dict):
        report.fail("persisted transcription job is missing")
    elif transcription_job.get("status") != "SUCCEEDED":
        report.fail(
            f"persisted transcription job is not SUCCEEDED: {transcription_job.get('status')}"
        )
    return report


def _read_object(path: Path, label: str, report: ValidationReport) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        report.fail(f"{label} cannot be read: {exc}")
        return None
    if not isinstance(value, dict):
        report.fail(f"{label} root is not an object")
        return None
    return value


def _find_track(
    tracks: dict[str, object],
    candidates: tuple[str, ...],
) -> tuple[str | None, dict[str, object] | None]:
    for name in candidates:
        value = tracks.get(name)
        if isinstance(value, dict):
            return name, value
    return None, None


def _resolve_audio_path(session: Path, value: object) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    relative = Path(value)
    if relative.is_absolute():
        return None
    if relative.parts == (relative.name,):
        return session / "audio" / relative
    if relative.parts == ("audio", relative.name):
        return session / relative
    return None


def _wave_metrics(path: Path) -> dict[str, object]:
    with wave.open(str(path), "rb") as stream:
        channels = stream.getnchannels()
        sample_width = stream.getsampwidth()
        sample_rate = stream.getframerate()
        frame_count = stream.getnframes()
        if sample_width != 2:
            raise ValueError("only PCM16 WAV is supported")
        peak = 0.0
        square_sum = 0.0
        sample_count = 0
        while frames := stream.readframes(max(sample_rate, 1)):
            samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
            if not samples.size:
                continue
            peak = max(peak, float(np.max(np.abs(samples))))
            square_sum += float(np.square(samples, dtype=np.float64).sum())
            sample_count += int(samples.size)
    rms = math.sqrt(square_sum / sample_count) if sample_count else 0.0
    return {
        "channels": channels,
        "sample_rate": sample_rate,
        "frame_count": frame_count,
        "duration_seconds": frame_count / sample_rate if sample_rate else 0.0,
        "peak": round(peak, 6),
        "rms": round(rms, 6),
    }


def _valid_timestamp(start: object, end: object) -> bool:
    return (
        isinstance(start, (int, float))
        and not isinstance(start, bool)
        and isinstance(end, (int, float))
        and not isinstance(end, bool)
        and math.isfinite(start)
        and math.isfinite(end)
        and 0 <= start <= end
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a recorded Phase 2 session")
    parser.add_argument("--session", required=True, type=Path)
    parser.add_argument("--expect-microphone")
    parser.add_argument("--expect-system")
    args = parser.parse_args(argv)
    report = validate_phase2_session(
        args.session,
        expected_microphone_text=args.expect_microphone,
        expected_system_text=args.expect_system,
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
