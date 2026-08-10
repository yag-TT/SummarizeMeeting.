"""録音セッションと各取得コンポーネントの永続状態モデル。"""

from __future__ import annotations

import platform
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

SESSION_SCHEMA_VERSION = 2
AUDIO_MANIFEST_SCHEMA_VERSION = 3


class SessionStatus(StrEnum):
    CREATED = "CREATED"
    PREPARING = "PREPARING"
    RECORDING = "RECORDING"
    STOPPING = "STOPPING"
    FINALIZING = "FINALIZING"
    RECORDED = "RECORDED"
    INTERRUPTED = "INTERRUPTED"
    FAILED_TO_START = "FAILED_TO_START"


class ComponentKind(StrEnum):
    MICROPHONE = "microphone"
    SYSTEM_AUDIO = "system_audio"
    SCREEN = "screen"
    SESSION_STORAGE = "session_storage"


class ComponentStatus(StrEnum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    READY = "READY"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    RECONNECTING = "RECONNECTING"
    PAUSED = "PAUSED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


@dataclass(slots=True)
class ComponentState:
    status: ComponentStatus = ComponentStatus.NOT_CONFIGURED
    error_code: str | None = None
    message: str | None = None


@dataclass(slots=True)
class RecordingSession:
    """会議の入力設定、状態遷移、警告をsession.json向けに保持する。"""

    title: str
    id: str = field(default_factory=lambda: str(uuid4()))
    schema_version: int = SESSION_SCHEMA_VERSION
    status: SessionStatus = SessionStatus.CREATED
    started_at: str | None = None
    ended_at: str | None = None
    duration_ms: int | None = None
    monotonic_origin_ns: int | None = None
    audio: dict[str, Any] = field(default_factory=dict)
    screen: dict[str, Any] = field(default_factory=dict)
    components: dict[str, ComponentState] = field(
        default_factory=lambda: {kind.value: ComponentState() for kind in ComponentKind}
    )
    retention: dict[str, bool] = field(
        default_factory=lambda: {"keep_audio": True, "keep_screenshots": True}
    )
    warnings: list[dict[str, Any]] = field(default_factory=list)

    def set_component(
        self,
        kind: ComponentKind,
        status: ComponentStatus,
        *,
        error_code: str | None = None,
        message: str | None = None,
    ) -> None:
        self.components[kind.value] = ComponentState(status, error_code, message)

    def add_warning(self, code: str, message: str, timestamp_ms: int) -> None:
        self.warnings.append({"code": code, "message": message, "timestamp_ms": timestamp_ms})

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["platform"] = {
            "system": platform.system(),
            "release": platform.release(),
        }
        return value

    @staticmethod
    def now_iso() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")
