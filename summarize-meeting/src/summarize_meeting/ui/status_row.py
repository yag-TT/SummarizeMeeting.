from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QProgressBar, QWidget

_COLORS = {
    "NOT_CONFIGURED": "#808080",
    "READY": "#808080",
    "STARTING": "#d4a017",
    "RUNNING": "#2e9d50",
    "RECONNECTING": "#d4a017",
    "PAUSED": "#d4a017",
    "STOPPING": "#d4a017",
    "STOPPED": "#808080",
    "FAILED": "#d64545",
}

_TEXT = {
    "NOT_CONFIGURED": "未選択",
    "READY": "待機",
    "STARTING": "接続中",
    "RUNNING": "取得中",
    "RECONNECTING": "再接続中",
    "PAUSED": "一時停止",
    "STOPPING": "停止処理中",
    "STOPPED": "停止",
    "FAILED": "取得失敗",
}


class CaptureStatusRow(QWidget):
    def __init__(self, title: str, *, show_meter: bool = True) -> None:
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._lamp = QLabel("●")
        self._lamp.setFixedWidth(18)
        self._title = QLabel(title)
        self._title.setMinimumWidth(90)
        self._status = QLabel("未選択")
        self._status.setMinimumWidth(90)
        self._meter = QProgressBar()
        self._meter.setRange(0, 1000)
        self._meter.setTextVisible(False)
        self._meter.setMinimumWidth(180)
        self._detail = QLabel("")
        self._detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self._lamp)
        layout.addWidget(self._title)
        layout.addWidget(self._status)
        if show_meter:
            layout.addWidget(self._meter, 1)
        else:
            self._meter.hide()
            layout.addStretch(1)
        layout.addWidget(self._detail, 1)
        self.set_state("NOT_CONFIGURED")

    def set_state(self, state: str, detail: str = "") -> None:
        color = _COLORS.get(state, "#808080")
        self._lamp.setStyleSheet(f"color: {color}; font-size: 18px;")
        self._status.setText(_TEXT.get(state, state))
        self._detail.setText(detail)
        if state in {"FAILED", "STOPPED", "NOT_CONFIGURED"}:
            self.set_level(0.0)

    def set_level(self, level: float) -> None:
        self._meter.setValue(round(max(0.0, min(1.0, level)) * 1000))
