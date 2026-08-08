import numpy as np

from summarize_meeting.capture.screen.change_detector import ScreenChangeDetector


def test_detector_saves_initial_and_stable_large_change() -> None:
    detector = ScreenChangeDetector(debounce_ms=500)
    initial = np.zeros((180, 320, 3), dtype=np.uint8)
    changed = np.full((180, 320, 3), 255, dtype=np.uint8)

    first = detector.evaluate(initial, 0)
    assert first is not None
    assert first.reason == "initial"
    detector.mark_saved(first)

    assert detector.evaluate(initial, 500) is None
    assert detector.evaluate(changed, 1_000) is None
    decision = detector.evaluate(changed, 1_500)
    assert decision is not None
    assert decision.reason == "stable_change"


def test_detector_ignores_small_cursor_sized_change() -> None:
    detector = ScreenChangeDetector()
    initial = np.zeros((180, 320, 3), dtype=np.uint8)
    cursor = initial.copy()
    cursor[10:15, 10:15] = 255
    first = detector.evaluate(initial, 0)
    assert first is not None
    detector.mark_saved(first)

    assert detector.evaluate(cursor, 1_000) is None
