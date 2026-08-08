from __future__ import annotations

import json
from pathlib import Path

import pytest

from summarize_meeting.infrastructure import settings as settings_module
from summarize_meeting.infrastructure.settings import (
    AppSettings,
    FileSettingsRepository,
    RetentionSettings,
    ScreenChangeSettings,
)


def test_missing_settings_uses_defaults(tmp_path: Path) -> None:
    result = FileSettingsRepository(tmp_path / "settings.json").load()

    assert result.settings == AppSettings()
    assert result.backup_path is None


def test_settings_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    repository = FileSettingsRepository(path)
    expected = AppSettings(
        last_microphone_device_id="mic-1",
        last_system_device_id="speaker-1",
        screen_evaluation_fps=1.5,
        screen_change_thresholds=ScreenChangeSettings(
            pixel_diff_threshold=20,
            changed_area_ratio_threshold=0.05,
            mean_abs_diff_threshold=6.0,
            debounce_ms=750,
            stable_changed_area_ratio=0.02,
            timeout_ms=7_500,
        ),
        retention=RetentionSettings(keep_audio=True, keep_screenshots=True),
        auto_transcribe_after_recording=True,
        log_level="DEBUG",
    )

    repository.save(expected)
    result = repository.load()

    assert result.settings == expected
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 1
    assert result.settings.auto_transcribe_after_recording


def test_old_settings_default_auto_transcription_to_disabled(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{"schema_version":1}', encoding="utf-8")

    result = FileSettingsRepository(path).load()

    assert not result.settings.auto_transcribe_after_recording


def test_corrupt_settings_are_backed_up_without_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    original = b'{"screen_evaluation_fps": invalid}'
    path.write_bytes(original)

    result = FileSettingsRepository(path).load()

    assert result.settings == AppSettings()
    assert result.error is not None
    assert result.backup_path is not None
    assert result.backup_path.read_bytes() == original
    assert not path.exists()


def test_invalid_setting_value_is_treated_as_corrupt(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{"screen_evaluation_fps": 0}', encoding="utf-8")

    result = FileSettingsRepository(path).load()

    assert result.settings == AppSettings()
    assert result.backup_path is not None


def test_corrupt_original_remains_when_backup_fails(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "settings.json"
    original = b"not-json"
    path.write_bytes(original)

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("backup failed")

    monkeypatch.setattr(settings_module.os, "replace", fail_replace)

    result = FileSettingsRepository(path).load()

    assert result.settings == AppSettings()
    assert result.backup_path is None
    assert path.read_bytes() == original


def test_atomic_save_failure_leaves_previous_settings(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "settings.json"
    repository = FileSettingsRepository(path)
    original = AppSettings(log_level="INFO")
    repository.save(original)

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(settings_module.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        repository.save(AppSettings(log_level="DEBUG"))

    assert repository.load().settings == original
