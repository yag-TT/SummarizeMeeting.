from __future__ import annotations

import json
import shutil
import wave
from pathlib import Path

import numpy as np
import pytest

from summarize_meeting.processing.audio_enhancement import (
    AudioEnhancementError,
    AudioEnhancementService,
)


class _CopyBackend:
    model_name = "fake.onnx"
    model_sha256 = "ABC123"

    def enhance(self, source: Path, output: Path, *, progress_callback=None) -> None:
        if progress_callback is not None:
            progress_callback(0.5)
        shutil.copyfile(source, output)


class _FailingBackend(_CopyBackend):
    def enhance(self, source: Path, output: Path, *, progress_callback=None) -> None:
        output.write_bytes(b"partial")
        raise RuntimeError("backend failed")


def test_service_preserves_raw_audio_and_writes_enhanced_metadata(tmp_path: Path) -> None:
    session = _session(tmp_path)
    source = session / "audio" / "microphone.wav"
    original = source.read_bytes()
    progress: list[tuple[int, str]] = []

    output = AudioEnhancementService(_CopyBackend()).run(
        session,
        progress_callback=lambda percent, message: progress.append((percent, message)),
    )

    assert source.read_bytes() == original
    assert output == session / "audio" / "microphone.enhanced.wav"
    assert output.read_bytes() == original
    metadata = json.loads(
        (session / "analysis" / "audio_enhancement.json").read_text("utf-8")
    )
    assert metadata["status"] == "SUCCEEDED"
    assert metadata["source_file"] == "audio/microphone.wav"
    assert metadata["output_file"] == "audio/microphone.enhanced.wav"
    assert metadata["model"] == "fake.onnx"
    assert progress[-1][0] == 100


def test_failed_rerun_keeps_previous_enhanced_audio_and_metadata(tmp_path: Path) -> None:
    session = _session(tmp_path)
    service = AudioEnhancementService(_CopyBackend())
    output = service.run(session)
    previous_audio = output.read_bytes()
    metadata_path = session / "analysis" / "audio_enhancement.json"
    previous_metadata = metadata_path.read_bytes()

    with pytest.raises(RuntimeError, match="backend failed"):
        AudioEnhancementService(_FailingBackend()).run(session)

    assert output.read_bytes() == previous_audio
    assert metadata_path.read_bytes() == previous_metadata
    assert not (session / "audio" / ".microphone.enhanced.wav.tmp").exists()


def test_service_rejects_microphone_path_outside_audio_directory(tmp_path: Path) -> None:
    session = _session(tmp_path)
    manifest_path = session / "audio" / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["tracks"]["microphone"]["file"] = "../outside.wav"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(AudioEnhancementError, match="ファイル名が不正"):
        AudioEnhancementService(_CopyBackend()).run(session)


def test_service_rejects_corrupt_wave_without_replacing_previous_output(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path)
    output = session / "audio" / "microphone.enhanced.wav"
    output.write_bytes(b"previous")
    (session / "audio" / "microphone.wav").write_bytes(b"broken")

    with pytest.raises(AudioEnhancementError, match="WAVを検証できません"):
        AudioEnhancementService(_CopyBackend()).run(session)

    assert output.read_bytes() == b"previous"


def _session(tmp_path: Path) -> Path:
    session = tmp_path / "session"
    audio = session / "audio"
    audio.mkdir(parents=True)
    (session / "analysis").mkdir()
    (audio / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "tracks": {
                    "microphone": {
                        "file": "audio/microphone.wav",
                        "estimated_start_offset_ms": 0,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    samples = (np.sin(np.arange(4_800) * 2 * np.pi * 440 / 48_000) * 0.1 * 32767).astype(
        "<i2"
    )
    with wave.open(str(audio / "microphone.wav"), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(48_000)
        stream.writeframes(samples.tobytes())
    return session
