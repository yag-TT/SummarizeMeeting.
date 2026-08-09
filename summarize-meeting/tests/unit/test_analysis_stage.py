from PySide6.QtWidgets import QApplication

from summarize_meeting.ui.analysis_stage import AnalysisStageCard, AnalysisStageState


def test_analysis_stage_exposes_text_and_accessible_state(qapp: QApplication) -> None:
    stage = AnalysisStageCard("3A", "話者分離", optional=True)

    stage.set_state(
        AnalysisStageState.RUNNING,
        detail="話者埋め込みを計算しています。",
    )

    assert stage.state == AnalysisStageState.RUNNING
    assert stage.property("stageState") == "running"
    assert stage.status_label.text() == "実行中"
    assert "実行中" in stage.status_label.accessibleName()
    assert "話者埋め込み" in stage.detail_label.accessibleName()
    assert "#a97f1e" in stage.styleSheet()
    stage.close()


def test_analysis_stage_can_show_specific_status_text(qapp: QApplication) -> None:
    stage = AnalysisStageCard("1", "会議終了・録音保存")

    stage.set_state(
        AnalysisStageState.WAITING,
        detail="会議終了後に保存します。",
        status_text="会議中",
    )

    assert stage.state == AnalysisStageState.WAITING
    assert stage.status_label.text() == "会議中"
    assert "会議中" in stage.toolTip()
    stage.close()
