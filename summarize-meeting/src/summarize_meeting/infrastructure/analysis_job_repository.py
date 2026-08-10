"""録音後解析ジョブの最新状態をjobs.jsonへ原子的に保存する。"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from summarize_meeting.domain.analysis_job import AnalysisJobState


class FileAnalysisJobRepository:
    """複数種類の解析状態を、既存項目を保ったまま1ファイルへ統合する。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def save(self, session_directory: Path, state: AnalysisJobState) -> Path:
        path = session_directory / "analysis" / "jobs.json"
        with self._lock:
            value = self._load_value(path)
            jobs = value.get("jobs")
            if not isinstance(jobs, dict):
                jobs = {}
            jobs[state.job] = state.to_dict()
            value = {"schema_version": 1, "jobs": jobs}
            self._write_json_atomic(path, value)
        return path

    @staticmethod
    def _load_value(path: Path) -> dict[str, object]:
        if not path.is_file():
            return {"schema_version": 1, "jobs": {}}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"schema_version": 1, "jobs": {}}
        return value if isinstance(value, dict) else {"schema_version": 1, "jobs": {}}

    @staticmethod
    def _write_json_atomic(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
