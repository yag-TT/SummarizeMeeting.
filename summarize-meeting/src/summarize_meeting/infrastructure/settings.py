from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}


@dataclass(frozen=True, slots=True)
class ScreenChangeSettings:
    pixel_diff_threshold: int = 16
    changed_area_ratio_threshold: float = 0.03
    mean_abs_diff_threshold: float = 4.0
    debounce_ms: int = 500
    stable_changed_area_ratio: float = 0.01
    timeout_ms: int = 5_000


@dataclass(frozen=True, slots=True)
class RetentionSettings:
    keep_audio: bool = True
    keep_screenshots: bool = True


@dataclass(frozen=True, slots=True)
class AppSettings:
    schema_version: int = 1
    last_microphone_device_id: str | None = None
    last_system_device_id: str | None = None
    screen_evaluation_fps: float = 2.0
    screen_change_thresholds: ScreenChangeSettings = field(default_factory=ScreenChangeSettings)
    retention: RetentionSettings = field(default_factory=RetentionSettings)
    auto_transcribe_after_recording: bool = False
    log_level: str = "INFO"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: object) -> AppSettings:
        if not isinstance(value, dict):
            raise ValueError("settings root must be an object")
        schema_version = _integer(value.get("schema_version", 1), "schema_version", 1, 1)
        screen = value.get("screen_change_thresholds", {})
        retention = value.get("retention", {})
        if not isinstance(screen, dict):
            raise ValueError("screen_change_thresholds must be an object")
        if not isinstance(retention, dict):
            raise ValueError("retention must be an object")
        log_level = value.get("log_level", "INFO")
        if not isinstance(log_level, str) or log_level.upper() not in _LOG_LEVELS:
            raise ValueError("log_level is invalid")
        return cls(
            schema_version=schema_version,
            last_microphone_device_id=_optional_string(
                value.get("last_microphone_device_id"),
                "last_microphone_device_id",
            ),
            last_system_device_id=_optional_string(
                value.get("last_system_device_id"),
                "last_system_device_id",
            ),
            screen_evaluation_fps=_number(
                value.get("screen_evaluation_fps", 2.0),
                "screen_evaluation_fps",
                0.1,
                10.0,
            ),
            screen_change_thresholds=ScreenChangeSettings(
                pixel_diff_threshold=_integer(
                    screen.get("pixel_diff_threshold", 16),
                    "pixel_diff_threshold",
                    1,
                    255,
                ),
                changed_area_ratio_threshold=_number(
                    screen.get("changed_area_ratio_threshold", 0.03),
                    "changed_area_ratio_threshold",
                    0.0,
                    1.0,
                ),
                mean_abs_diff_threshold=_number(
                    screen.get("mean_abs_diff_threshold", 4.0),
                    "mean_abs_diff_threshold",
                    0.0,
                    255.0,
                ),
                debounce_ms=_integer(
                    screen.get("debounce_ms", 500),
                    "debounce_ms",
                    0,
                    60_000,
                ),
                stable_changed_area_ratio=_number(
                    screen.get("stable_changed_area_ratio", 0.01),
                    "stable_changed_area_ratio",
                    0.0,
                    1.0,
                ),
                timeout_ms=_integer(
                    screen.get("timeout_ms", 5_000),
                    "timeout_ms",
                    1,
                    300_000,
                ),
            ),
            retention=RetentionSettings(
                keep_audio=_boolean(retention.get("keep_audio", True), "keep_audio"),
                keep_screenshots=_boolean(
                    retention.get("keep_screenshots", True),
                    "keep_screenshots",
                ),
            ),
            auto_transcribe_after_recording=_boolean(
                value.get("auto_transcribe_after_recording", False),
                "auto_transcribe_after_recording",
            ),
            log_level=log_level.upper(),
        )


@dataclass(frozen=True, slots=True)
class SettingsLoadResult:
    settings: AppSettings
    backup_path: Path | None = None
    error: str | None = None


class FileSettingsRepository:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> SettingsLoadResult:
        if not self._path.exists():
            return SettingsLoadResult(AppSettings())
        try:
            value = json.loads(self._path.read_text(encoding="utf-8"))
            return SettingsLoadResult(AppSettings.from_dict(value))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            backup_path = self._backup_invalid_file()
            return SettingsLoadResult(
                AppSettings(),
                backup_path=backup_path,
                error=str(exc),
            )
        except OSError as exc:
            return SettingsLoadResult(AppSettings(), error=str(exc))

    def save(self, settings: AppSettings) -> None:
        temporary = self._path.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(settings.to_dict(), stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self._path)

    def _backup_invalid_file(self) -> Path | None:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup_path = self._path.with_name(
            f"{self._path.stem}.corrupt-{timestamp}{self._path.suffix}"
        )
        try:
            os.replace(self._path, backup_path)
        except OSError:
            return None
        return backup_path


def _optional_string(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string or null")
    return value


def _number(value: object, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    result = float(value)
    if not minimum <= result <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return result


def _integer(value: object, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value
