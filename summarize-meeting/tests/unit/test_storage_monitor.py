from __future__ import annotations

import threading
from collections import deque
from pathlib import Path

import pytest

from summarize_meeting.application.storage_monitor import (
    InsufficientDiskSpaceError,
    StorageCapacityCheckError,
    StorageMonitor,
)


class _SequenceProbe:
    def __init__(self, *results: int | OSError) -> None:
        self._results = deque(results)

    def free_bytes(self, path: Path) -> int:
        result = self._results.popleft()
        if isinstance(result, OSError):
            raise result
        return result


def test_preflight_rejects_capacity_below_minimum(tmp_path: Path) -> None:
    monitor = StorageMonitor(
        path=tmp_path,
        probe=_SequenceProbe(99),
        minimum_free_bytes=100,
    )

    with pytest.raises(InsufficientDiskSpaceError) as raised:
        monitor.check_start_allowed()

    assert raised.value.capacity.free_bytes == 99
    assert raised.value.capacity.minimum_free_bytes == 100


def test_preflight_allows_capacity_equal_to_minimum(tmp_path: Path) -> None:
    monitor = StorageMonitor(
        path=tmp_path,
        probe=_SequenceProbe(100),
        minimum_free_bytes=100,
    )

    capacity = monitor.check_start_allowed()

    assert not capacity.is_low


def test_preflight_adds_requested_enhanced_audio_reserve(tmp_path: Path) -> None:
    monitor = StorageMonitor(
        path=tmp_path,
        probe=_SequenceProbe(149),
        minimum_free_bytes=100,
    )

    with pytest.raises(InsufficientDiskSpaceError) as raised:
        monitor.check_start_allowed(additional_required_bytes=50)

    assert raised.value.capacity.minimum_free_bytes == 150


def test_monitor_reports_low_capacity_once(tmp_path: Path) -> None:
    monitor = StorageMonitor(
        path=tmp_path,
        probe=_SequenceProbe(99),
        minimum_free_bytes=100,
        check_interval_seconds=0.01,
    )
    reported = threading.Event()
    capacities = []

    monitor.start(
        low_capacity_callback=lambda capacity: (capacities.append(capacity), reported.set()),
        check_failed_callback=lambda error: pytest.fail(str(error)),
    )

    assert reported.wait(1.0)
    monitor.finish()
    assert [capacity.free_bytes for capacity in capacities] == [99]


def test_monitor_reports_probe_failure(tmp_path: Path) -> None:
    monitor = StorageMonitor(
        path=tmp_path,
        probe=_SequenceProbe(OSError("drive unavailable")),
        minimum_free_bytes=100,
        check_interval_seconds=0.01,
    )
    reported = threading.Event()
    errors: list[StorageCapacityCheckError] = []

    monitor.start(
        low_capacity_callback=lambda capacity: pytest.fail(str(capacity)),
        check_failed_callback=lambda error: (errors.append(error), reported.set()),
    )

    assert reported.wait(1.0)
    monitor.finish()
    assert len(errors) == 1
    assert "空き容量を確認できません" in str(errors[0])
