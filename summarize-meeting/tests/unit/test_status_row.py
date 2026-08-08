from PySide6.QtWidgets import QApplication

from summarize_meeting.ui.status_row import CaptureStatusRow


def test_status_row_shows_source_name_and_full_name_tooltip(
    qapp: QApplication,
) -> None:
    row = CaptureStatusRow("マイク")
    source_name = "会議室の非常に長いUSBマイクデバイス名"

    row.set_source(source_name)

    assert row._source.text() == source_name  # noqa: SLF001
    assert row._source.toolTip() == source_name  # noqa: SLF001

    row.set_source(None)

    assert row._source.text() == "未選択"  # noqa: SLF001
    assert row._source.toolTip() == ""  # noqa: SLF001
