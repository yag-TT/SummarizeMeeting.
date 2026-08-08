from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from summarize_meeting.processing.screen_analysis import (
    ScreenAnalysisService,
    WindowsOcrBackend,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run local screenshot analysis")
    parser.add_argument("--session", required=True, type=Path)
    parser.add_argument("--language", default="ja")
    args = parser.parse_args(argv)
    service = ScreenAnalysisService(WindowsOcrBackend(language=args.language))

    def progress(percent: int, message: str) -> None:
        print(
            json.dumps(
                {"type": "progress", "percent": percent, "message": message},
                ensure_ascii=False,
            ),
            flush=True,
        )

    try:
        output = service.run(args.session, progress_callback=progress)
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1
    print(json.dumps({"type": "result", "path": str(output)}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
