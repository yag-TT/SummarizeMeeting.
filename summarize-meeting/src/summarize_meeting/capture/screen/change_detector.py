"""画面差分を縮小画像で評価し、安定した変化だけを保存対象として選ぶ。"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np
from numpy.typing import NDArray

from summarize_meeting.capture.screen.base import BgrFrame


@dataclass(frozen=True, slots=True)
class ChangeMetrics:
    changed_ratio: float
    mean_abs_diff: float


@dataclass(frozen=True, slots=True)
class ChangeDecision:
    reason: str
    metrics: ChangeMetrics
    signature: NDArray[np.uint8]


class ScreenChangeDetector:
    """一時的な描画途中をdebounceし、初回・安定変化・timeoutを判定する。"""

    def __init__(
        self,
        *,
        pixel_diff_threshold: int = 16,
        changed_area_ratio_threshold: float = 0.03,
        mean_abs_diff_threshold: float = 4.0,
        debounce_ms: int = 500,
        stable_changed_area_ratio: float = 0.01,
        timeout_ms: int = 5_000,
    ) -> None:
        self._pixel_diff_threshold = pixel_diff_threshold
        self._changed_area_ratio_threshold = changed_area_ratio_threshold
        self._mean_abs_diff_threshold = mean_abs_diff_threshold
        self._debounce_ms = debounce_ms
        self._stable_changed_area_ratio = stable_changed_area_ratio
        self._timeout_ms = timeout_ms
        self._baseline: NDArray[np.uint8] | None = None
        self._candidate: NDArray[np.uint8] | None = None
        self._candidate_started_ms: int | None = None

    def reset(self) -> None:
        self._baseline = None
        self._candidate = None
        self._candidate_started_ms = None

    def evaluate(self, frame: BgrFrame, timestamp_ms: int) -> ChangeDecision | None:
        signature = self._signature(frame)
        if self._baseline is None:
            return ChangeDecision("initial", ChangeMetrics(1.0, 255.0), signature)

        baseline_metrics = self._metrics(self._baseline, signature)
        if not self._is_meaningful(baseline_metrics):
            self._candidate = None
            self._candidate_started_ms = None
            return None

        if self._candidate is None:
            self._candidate = signature
            self._candidate_started_ms = timestamp_ms
            return None

        assert self._candidate_started_ms is not None
        age_ms = timestamp_ms - self._candidate_started_ms
        candidate_metrics = self._metrics(self._candidate, signature)
        if age_ms >= self._timeout_ms:
            return ChangeDecision("change_timeout", baseline_metrics, signature)
        if age_ms >= self._debounce_ms and (
            candidate_metrics.changed_ratio <= self._stable_changed_area_ratio
        ):
            return ChangeDecision("stable_change", baseline_metrics, signature)
        self._candidate = signature
        return None

    def mark_saved(self, decision: ChangeDecision) -> None:
        self._baseline = decision.signature.copy()
        self._candidate = None
        self._candidate_started_ms = None

    def _signature(self, frame: BgrFrame) -> NDArray[np.uint8]:
        height, width = frame.shape[:2]
        scale = min(320 / max(width, 1), 180 / max(height, 1), 1.0)
        size = (max(1, round(width * scale)), max(1, round(height * scale)))
        resized = cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
        return cv2.GaussianBlur(gray, (5, 5), 0)

    def _metrics(
        self,
        before: NDArray[np.uint8],
        after: NDArray[np.uint8],
    ) -> ChangeMetrics:
        if before.shape != after.shape:
            return ChangeMetrics(1.0, 255.0)
        difference = cv2.absdiff(before, after)
        changed_ratio = float(np.mean(difference >= self._pixel_diff_threshold))
        mean_abs_diff = float(np.mean(difference))
        return ChangeMetrics(changed_ratio, mean_abs_diff)

    def _is_meaningful(self, metrics: ChangeMetrics) -> bool:
        return (
            metrics.changed_ratio >= self._changed_area_ratio_threshold
            and metrics.mean_abs_diff >= self._mean_abs_diff_threshold
        )
