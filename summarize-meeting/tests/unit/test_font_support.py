from __future__ import annotations

from summarize_meeting.ui.font_support import select_japanese_font_family


def test_select_japanese_font_prefers_noto_sans_cjk_jp() -> None:
    selected = select_japanese_font_family(
        ["DejaVu Sans", "Noto Serif CJK JP", "Noto Sans CJK JP"]
    )

    assert selected == "Noto Sans CJK JP"


def test_select_japanese_font_uses_available_japanese_fallback() -> None:
    assert select_japanese_font_family(["Custom Japanese Font"]) == "Custom Japanese Font"


def test_select_japanese_font_reports_missing_support() -> None:
    assert select_japanese_font_family([]) is None
