from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication

_PREFERRED_JAPANESE_FONTS = (
    "Noto Sans CJK JP",
    "Noto Sans JP",
    "Yu Gothic UI",
    "Yu Gothic",
    "Meiryo UI",
    "Meiryo",
    "IPAexGothic",
    "IPAGothic",
)


def select_japanese_font_family(families: Iterable[str]) -> str | None:
    available = tuple(families)
    by_name = {family.casefold(): family for family in available}
    for preferred in _PREFERRED_JAPANESE_FONTS:
        if selected := by_name.get(preferred.casefold()):
            return selected
    return available[0] if available else None


def configure_japanese_ui_font(application: QApplication) -> str | None:
    families = QFontDatabase.families(QFontDatabase.WritingSystem.Japanese)
    selected = select_japanese_font_family(families)
    if selected is None:
        return None
    font = application.font()
    fallback = font.family()
    font.setFamilies([selected, fallback] if fallback != selected else [selected])
    application.setFont(font)
    return selected
