from __future__ import annotations

import shutil
from pathlib import Path


class SystemStorageProbe:
    def free_bytes(self, path: Path) -> int:
        existing_path = path
        while not existing_path.exists() and existing_path.parent != existing_path:
            existing_path = existing_path.parent
        return int(shutil.disk_usage(existing_path).free)
