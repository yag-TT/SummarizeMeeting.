from __future__ import annotations

import argparse
import json
import wave
from datetime import datetime
from pathlib import Path


def create_repeated_audio_session(
    source_session: Path,
    output_session: Path,
    *,
    duration_seconds: float,
) -> Path:
    if duration_seconds <= 0:
        raise ValueError("duration_seconds must be positive")
    source_session = source_session.resolve()
    output_session = output_session.resolve()
    if output_session.exists():
        raise FileExistsError(f"output session already exists: {output_session}")
    if source_session == output_session:
        raise ValueError("source and output sessions must differ")

    source_audio = source_session / "audio"
    manifest = _read_object(source_audio / "manifest.json")
    tracks = manifest.get("tracks")
    if not isinstance(tracks, dict) or not tracks:
        raise ValueError("source manifest has no tracks")

    output_audio = output_session / "audio"
    output_audio.mkdir(parents=True)
    (output_session / "analysis").mkdir()
    (output_session / "output").mkdir()
    output_tracks: dict[str, object] = {}
    for name, raw_track in tracks.items():
        if not isinstance(name, str) or not isinstance(raw_track, dict):
            continue
        file_name = raw_track.get("file")
        if not isinstance(file_name, str):
            raise ValueError(f"invalid track file: {name}")
        relative_path = Path(file_name)
        if relative_path.parts == (relative_path.name,):
            source_wave = source_audio / relative_path
        elif relative_path.parts == ("audio", relative_path.name):
            source_wave = source_session / relative_path
        else:
            raise ValueError(f"invalid track file: {name}")
        output_wave = output_audio / relative_path.name
        frames, sample_rate = _repeat_wave(
            source_wave,
            output_wave,
            duration_seconds=duration_seconds,
        )
        start_offset = raw_track.get("estimated_start_offset_ms")
        start_offset = start_offset if isinstance(start_offset, int) else 0
        output_tracks[name] = {
            **raw_track,
            "file": file_name,
            "frames_written": frames,
            "audio_duration_ms": frames * 1000 / sample_rate,
            "capture_ended_offset_ms": start_offset + round(frames * 1000 / sample_rate),
            "segments": 1,
            "validated": True,
        }

    (output_audio / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "monotonic_origin_ns": 0,
                "tracks": output_tracks,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    (output_session / "session.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": f"benchmark-{output_session.name}",
                "title": f"STT benchmark {duration_seconds:g}s",
                "status": "RECORDED",
                "started_at": now,
                "ended_at": now,
                "duration_ms": round(duration_seconds * 1000),
                "audio": {name: {} for name in output_tracks},
                "screen": {},
                "retention": {"keep_audio": True, "keep_screenshots": False},
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return output_session


def _repeat_wave(source: Path, output: Path, *, duration_seconds: float) -> tuple[int, int]:
    with wave.open(str(source), "rb") as reader:
        parameters = reader.getparams()
        seed_frames = reader.readframes(reader.getnframes())
    frame_width = parameters.nchannels * parameters.sampwidth
    if not seed_frames or len(seed_frames) % frame_width:
        raise ValueError(f"source WAV has no complete frames: {source}")
    seed_frame_count = len(seed_frames) // frame_width
    target_frames = max(1, round(duration_seconds * parameters.framerate))
    with wave.open(str(output), "wb") as writer:
        writer.setparams(parameters)
        remaining = target_frames
        while remaining > 0:
            count = min(seed_frame_count, remaining)
            writer.writeframesraw(seed_frames[: count * frame_width])
            remaining -= count
    return target_frames, parameters.framerate


def _read_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a repeated-audio STT benchmark session")
    parser.add_argument("--source-session", required=True, type=Path)
    parser.add_argument("--output-session", required=True, type=Path)
    parser.add_argument("--duration-seconds", required=True, type=float)
    args = parser.parse_args(argv)
    result = create_repeated_audio_session(
        args.source_session,
        args.output_session,
        duration_seconds=args.duration_seconds,
    )
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
