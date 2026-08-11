"""録音後解析ジョブの最新状態をjobs.jsonへ原子的に保存する。"""

from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path

from summarize_meeting.domain.analysis_job import AnalysisJobState
from summarize_meeting.infrastructure.atomic_io import write_json_atomic


class FileAnalysisJobRepository:
    """複数種類の解析状態を、既存項目を保ったまま1ファイルへ統合する。"""

    _locks_guard = threading.Lock()
    _locks_by_path: dict[str, threading.RLock] = {}

    def save(self, session_directory: Path, state: AnalysisJobState) -> Path:
        path = Path(os.path.abspath(session_directory / "analysis" / "jobs.json"))
        with self._lock_for(path):
            value = self._load_value(path)
            jobs = value.get("jobs")
            if not isinstance(jobs, dict):
                jobs = {}
            jobs[state.job] = state.to_dict()
            value = {"schema_version": 1, "jobs": jobs}
            write_json_atomic(path, value)
        return path

    @classmethod
    def _lock_for(cls, path: Path) -> threading.RLock:
        key = os.path.normcase(str(path)).removeprefix("\\\\?\\")
        with cls._locks_guard:
            return cls._locks_by_path.setdefault(key, threading.RLock())

    @staticmethod
    def _load_value(path: Path) -> dict[str, object]:
        if not path.is_file():
            return {"schema_version": 1, "jobs": {}}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            backup = path.with_name(f"{path.name}.corrupt-{uuid.uuid4().hex}")
            os.replace(path, backup)
            return {"schema_version": 1, "jobs": {}}
        return value if isinstance(value, dict) else {"schema_version": 1, "jobs": {}}
