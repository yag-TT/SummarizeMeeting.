from __future__ import annotations

import json
import threading
from pathlib import Path

from summarize_meeting.infrastructure.session_log import SessionLogWriter


def test_session_log_redacts_registered_values_and_sensitive_fields(
    tmp_path: Path,
) -> None:
    path = tmp_path / "session.log"
    secret_title = "極秘プロジェクト定例"
    secret_device_id = "secret-device-id"
    secret_device_name = "Confidential Headset"
    secret_root = tmp_path / secret_title

    with SessionLogWriter(
        path,
        session_id="session-1",
        sensitive_values=(
            secret_title,
            secret_device_id,
            secret_device_name,
            secret_root,
        ),
    ) as writer:
        writer.write(
            "component_state_changed",
            component="microphone",
            status="FAILED",
            title=secret_title,
            device_id=secret_device_id,
            message=f"{secret_device_name} at {secret_root} failed",
            nested={"target": secret_title, "count": 3},
        )
        try:
            raise RuntimeError(
                f"capture failed for {secret_device_id} in {secret_root}"
            )
        except RuntimeError as exc:
            writer.write_exception(
                "capture_failed",
                exc,
                component="microphone",
                error_code="AUDIO_CAPTURE_FAILED",
            )

    raw = path.read_text(encoding="utf-8")
    assert secret_title not in raw
    assert secret_device_id not in raw
    assert secret_device_name not in raw
    assert str(secret_root) not in raw
    entries = [json.loads(line) for line in raw.splitlines()]
    assert entries[0]["details"]["title"] == "[REDACTED]"
    assert entries[0]["details"]["device_id"] == "[REDACTED]"
    assert entries[0]["details"]["nested"]["target"] == "[REDACTED]"
    assert entries[1]["details"]["exception_type"] == "RuntimeError"
    assert entries[1]["details"]["error_code"] == "AUDIO_CAPTURE_FAILED"
    assert "[REDACTED]" in entries[1]["details"]["stack_trace"]


def test_session_log_supports_concurrent_json_line_writes(tmp_path: Path) -> None:
    path = tmp_path / "session.log"
    writer = SessionLogWriter(path, session_id="session-2")

    def write_events(worker: int) -> None:
        for sequence in range(25):
            writer.write("worker_event", worker=worker, sequence=sequence)

    threads = [threading.Thread(target=write_events, args=(worker,)) for worker in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    writer.close()

    entries = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(entries) == 100
    assert all(entry["event"] == "worker_event" for entry in entries)


def test_session_log_respects_minimum_level(tmp_path: Path) -> None:
    path = tmp_path / "session.log"
    with SessionLogWriter(
        path,
        session_id="session-3",
        minimum_level="WARNING",
    ) as writer:
        writer.write("not_saved", level="INFO")
        writer.write("saved", level="WARNING")

    entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [entry["event"] for entry in entries] == ["saved"]


def test_session_log_write_failure_does_not_escape_to_capture_caller(
    tmp_path: Path,
) -> None:
    writer = SessionLogWriter(tmp_path / "session.log", session_id="session-4")

    written = writer.write("invalid_unicode", message="\udcff")

    assert not written
    assert isinstance(writer.write_error, UnicodeEncodeError)
    assert not writer.write("ignored_after_failure")
    writer.close()
