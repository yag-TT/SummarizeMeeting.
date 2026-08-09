from pathlib import Path

import pytest

from summarize_meeting.processing.minutes_worker import main


def test_minutes_worker_requires_base_url(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--session", str(tmp_path / "session")])

    assert exc_info.value.code == 2
