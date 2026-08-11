"""一時ファイルを用いた単一・複数成果物の安全な公開処理。"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Mapping
from pathlib import Path


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def write_json_atomic(path: Path, value: object) -> None:
    write_bytes_atomic(path, json_bytes(value))


def write_text_atomic(path: Path, content: str) -> None:
    write_bytes_atomic(path, content.encode("utf-8"))


def write_bytes_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary_path(path, "tmp")
    try:
        _write_durable(temporary, content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class ArtifactPublisher:
    """複数ファイルを準備してから公開し、失敗時は公開前の状態へ戻す。"""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def publish(self, artifacts: Mapping[Path, bytes]) -> None:
        if not artifacts:
            return
        targets = [(self._validated_target(path), content) for path, content in artifacts.items()]
        staged: dict[Path, Path] = {}
        backups: dict[Path, Path] = {}
        committed: list[Path] = []
        published = False
        try:
            for target, content in targets:
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = _temporary_path(target, "stage")
                _write_durable(temporary, content)
                staged[target] = temporary
            for target, _content in targets:
                if target.exists():
                    backup = _temporary_path(target, "backup")
                    os.replace(target, backup)
                    backups[target] = backup
                os.replace(staged[target], target)
                committed.append(target)
            published = True
        except Exception:
            self._rollback(committed, backups)
            raise
        finally:
            for temporary in staged.values():
                temporary.unlink(missing_ok=True)
            if published:
                for backup in backups.values():
                    backup.unlink(missing_ok=True)

    def publish_text(self, artifacts: Mapping[Path, str]) -> None:
        self.publish({path: content.encode("utf-8") for path, content in artifacts.items()})

    def _validated_target(self, path: Path) -> Path:
        target = path.resolve()
        try:
            target.relative_to(self._root)
        except ValueError as exc:
            raise ValueError("成果物の公開先が許可されたフォルダ外です") from exc
        return target

    @staticmethod
    def _rollback(committed: list[Path], backups: Mapping[Path, Path]) -> None:
        for target in reversed(committed):
            target.unlink(missing_ok=True)
            backup = backups.get(target)
            if backup is not None and backup.exists():
                os.replace(backup, target)
        for target, backup in backups.items():
            if target not in committed and backup.exists():
                os.replace(backup, target)


def _temporary_path(path: Path, suffix: str) -> Path:
    return path.with_name(f".{path.name}.{uuid.uuid4().hex}.{suffix}")


def _write_durable(path: Path, content: bytes) -> None:
    with path.open("wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
