from __future__ import annotations

import os
import sys
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4


class AppRootNotWritableError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PortableAppPaths:
    app_root: Path

    @classmethod
    def discover(cls) -> PortableAppPaths:
        override = os.environ.get("SUMMARIZE_MEETING_APP_ROOT")
        if override:
            return cls(Path(override).expanduser().resolve())
        if getattr(sys, "frozen", False):
            return cls(Path(sys.executable).resolve().parent)
        return cls(Path(__file__).resolve().parents[3])

    @property
    def data_dir(self) -> Path:
        return self.app_root / "data"

    @property
    def meetings_dir(self) -> Path:
        return self.data_dir / "meetings"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def settings_file(self) -> Path:
        return self.data_dir / "settings.json"

    @property
    def lock_file(self) -> Path:
        return self.data_dir / "instance.lock"

    def ensure_writable(self) -> None:
        probe: Path | None = None
        try:
            for path in (self.data_dir, self.meetings_dir, self.logs_dir):
                path.mkdir(parents=True, exist_ok=True)
            probe = self.data_dir / f".write-probe-{uuid4().hex}"
            with probe.open("xb") as stream:
                stream.write(b"ok")
                stream.flush()
                os.fsync(stream.fileno())
            probe.unlink()
            probe = None
        except OSError as exc:
            raise AppRootNotWritableError(
                f"アプリフォルダへ書き込めません: {self.app_root}"
            ) from exc
        finally:
            if probe is not None:
                with suppress(OSError):
                    probe.unlink()
