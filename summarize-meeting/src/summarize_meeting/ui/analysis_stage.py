from __future__ import annotations

from enum import StrEnum

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget


class AnalysisStageState(StrEnum):
    WAITING = "waiting"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    UNAVAILABLE = "unavailable"


_STATE_PRESENTATION = {
    AnalysisStageState.WAITING: ("待機中", "#aeb4bd", "#505965"),
    AnalysisStageState.READY: ("実行可能", "#8ecaff", "#3977a8"),
    AnalysisStageState.RUNNING: ("実行中", "#ffd166", "#a97f1e"),
    AnalysisStageState.COMPLETED: ("完了", "#8bd6a8", "#3c8a5d"),
    AnalysisStageState.FAILED: ("失敗", "#ffaaa3", "#a64d52"),
    AnalysisStageState.CANCELED: ("キャンセル", "#e3c28c", "#876a3d"),
    AnalysisStageState.UNAVAILABLE: ("対象なし", "#8e969f", "#444b54"),
}


class AnalysisStageCard(QWidget):
    def __init__(
        self,
        number: str,
        title: str,
        *,
        optional: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._title = title
        self._state = AnalysisStageState.WAITING
        self.setObjectName("analysisStageCard")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAccessibleName(f"工程 {number}: {title}")

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 12)
        root.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(8)
        number_label = QLabel(number)
        number_label.setObjectName("analysisStageNumber")
        number_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        number_label.setFixedSize(26, 26)
        number_label.setAccessibleName(f"工程番号 {number}")
        header.addWidget(number_label)
        title_label = QLabel(title)
        title_label.setObjectName("analysisStageTitle")
        header.addWidget(title_label)
        if optional:
            optional_label = QLabel("任意")
            optional_label.setObjectName("analysisStageOptional")
            optional_label.setToolTip("完了すると会話要約へ追加情報として反映されます")
            header.addWidget(optional_label)
        header.addStretch(1)
        self.status_label = QLabel()
        self.status_label.setObjectName("analysisStageStatus")
        header.addWidget(self.status_label)
        root.addLayout(header)

        self.detail_label = QLabel()
        self.detail_label.setObjectName("analysisStageDetail")
        self.detail_label.setWordWrap(True)
        root.addWidget(self.detail_label)

        self.body_layout = QVBoxLayout()
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(7)
        root.addLayout(self.body_layout)

        self.set_state(
            AnalysisStageState.WAITING,
            detail="前の工程が完了するまでお待ちください。",
        )

    @property
    def state(self) -> AnalysisStageState:
        return self._state

    def set_state(
        self,
        state: AnalysisStageState,
        *,
        detail: str,
        status_text: str | None = None,
    ) -> None:
        self._state = state
        default_text, text_color, border_color = _STATE_PRESENTATION[state]
        visible_text = status_text or default_text
        self.status_label.setText(visible_text)
        self.status_label.setAccessibleName(f"{self._title}の状態: {visible_text}")
        self.detail_label.setText(detail)
        self.detail_label.setAccessibleName(f"{self._title}の説明: {detail}")
        self.setProperty("stageState", state.value)
        self.setToolTip(f"{self._title}: {visible_text}\n{detail}")
        self.setStyleSheet(
            f"""
            QWidget#analysisStageCard {{
                background-color: #1f252d;
                border: 2px solid {border_color};
                border-radius: 8px;
            }}
            QWidget#analysisStageCard QLabel {{
                color: #e0e5eb;
                border: none;
            }}
            QLabel#analysisStageNumber {{
                background-color: {border_color};
                color: #ffffff;
                border: none;
                border-radius: 13px;
                font-weight: 700;
            }}
            QLabel#analysisStageTitle {{
                color: #f2f4f7;
                border: none;
                font-size: 14px;
                font-weight: 700;
            }}
            QLabel#analysisStageOptional {{
                color: #c7cdd4;
                background-color: #343c46;
                border: 1px solid #59636f;
                border-radius: 3px;
                padding: 1px 5px;
            }}
            QLabel#analysisStageStatus {{
                color: {text_color};
                border: none;
                font-weight: 700;
            }}
            QLabel#analysisStageDetail {{
                color: #c7cdd4;
                border: none;
            }}
            """
        )
