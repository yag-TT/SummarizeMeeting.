from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

MINIMUM_FREE_BYTES = 5 * 1024**3
DEFAULT_CHECK_INTERVAL_SECONDS = 60.0
ENHANCED_MICROPHONE_RESERVE_BYTES = 48_000 * 2 * 60 * 60


class StorageProbe(Protocol):
    def free_bytes(self, path: Path) -> int: ...


@dataclass(frozen=True, slots=True)
class StorageCapacity:
    free_bytes: int
    minimum_free_bytes: int

    @property
    def is_low(self) -> bool:
        return self.free_bytes < self.minimum_free_bytes


class InsufficientDiskSpaceError(RuntimeError):
    def __init__(self, capacity: StorageCapacity) -> None:
        self.capacity = capacity
        super().__init__(
            "保存先の空き容量が不足しています。"
            f"空き {format_gib(capacity.free_bytes)} GiB、"
            f"必要 {format_gib(capacity.minimum_free_bytes)} GiB です。"
        )


class StorageCapacityCheckError(RuntimeError):
    pass


LowCapacityCallback = Callable[[StorageCapacity], None]
CheckFailedCallback = Callable[[StorageCapacityCheckError], None]


class StorageMonitor:
    def __init__(
        self,
        *,
        path: Path,
        probe: StorageProbe,
        minimum_free_bytes: int = MINIMUM_FREE_BYTES,
        check_interval_seconds: float = DEFAULT_CHECK_INTERVAL_SECONDS,
    ) -> None:
        if minimum_free_bytes <= 0:
            raise ValueError("minimum_free_bytes must be positive")
        if check_interval_seconds <= 0:
            raise ValueError("check_interval_seconds must be positive")
        self._path = path
        self._probe = probe
        self._minimum_free_bytes = minimum_free_bytes
        self._check_interval_seconds = check_interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def check_start_allowed(self, *, additional_required_bytes: int = 0) -> StorageCapacity:
        if additional_required_bytes < 0:
            raise ValueError("additional_required_bytes must not be negative")
        capacity = self.check()
        adjusted = StorageCapacity(
            free_bytes=capacity.free_bytes,
            minimum_free_bytes=capacity.minimum_free_bytes + additional_required_bytes,
        )
        if adjusted.is_low:
            raise InsufficientDiskSpaceError(adjusted)
        return adjusted

    def check(self) -> StorageCapacity:
        try:
            free_bytes = self._probe.free_bytes(self._path)
        except OSError as exc:
            raise StorageCapacityCheckError(
                f"保存先の空き容量を確認できません: {self._path}"
            ) from exc
        return StorageCapacity(
            free_bytes=max(0, int(free_bytes)),
            minimum_free_bytes=self._minimum_free_bytes,
        )

    def start(
        self,
        *,
        low_capacity_callback: LowCapacityCallback,
        check_failed_callback: CheckFailedCallback,
    ) -> None:
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("Storage monitor is already running")
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            args=(low_capacity_callback, check_failed_callback),
            name="storage-monitor",
            daemon=True,
        )
        self._thread.start()

    def request_stop(self) -> None:
        self._stop.set()

    def finish(self, timeout: float = 2.0) -> None:
        self.request_stop()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        if self._thread is not None and self._thread.is_alive():
            raise TimeoutError("Storage monitor did not stop")

    def _run(
        self,
        low_capacity_callback: LowCapacityCallback,
        check_failed_callback: CheckFailedCallback,
    ) -> None:
        while not self._stop.wait(self._check_interval_seconds):
            try:
                capacity = self.check()
            except StorageCapacityCheckError as exc:
                check_failed_callback(exc)
                return
            if capacity.is_low:
                low_capacity_callback(capacity)
                return


def format_gib(byte_count: int) -> str:
    return f"{byte_count / 1024**3:.2f}"
