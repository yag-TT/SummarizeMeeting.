import json
from pathlib import Path

from summarize_meeting.application.recording_controller import RecordingController
from summarize_meeting.infrastructure.audio_writer import AudioTrackStats


def test_audio_manifest_includes_origin_and_diagnostics(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    stats = AudioTrackStats(
        file="microphone.wav",
        sample_rate=48_000,
        channels=1,
        sample_width_bytes=2,
        frames_written=48_000,
        segments=1,
        estimated_start_offset_ms=12,
        capture_ended_offset_ms=1_020,
        audio_duration_ms=1_000.0,
        active_capture_duration_ms=1_008.0,
        duration_drift_ms=-8.0,
        overflow_count=0,
        queue_pressure_count=1,
        max_queue_usage_ratio=0.85,
        queue_capacity_chunks=300,
    )

    RecordingController._write_audio_manifest(  # noqa: SLF001
        path,
        {"microphone": stats},
        monotonic_origin_ns=123_456_789,
    )

    value = json.loads(path.read_text(encoding="utf-8"))
    assert value["schema_version"] == 2
    assert value["monotonic_origin_ns"] == 123_456_789
    track = value["tracks"]["microphone"]
    assert track["file"] == "microphone.wav"
    assert track["estimated_start_offset_ms"] == 12
    assert track["duration_drift_ms"] == -8.0
    assert track["queue_pressure_count"] == 1
