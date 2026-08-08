from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from summarize_meeting.processing.audio_enhancement import (
    MODEL_NAME,
    AudioEnhancementService,
    SherpaDpdfNetBackend,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Improve a recorded microphone track")
    parser.add_argument("--session", required=True, type=Path)
    parser.add_argument("--models-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    model = args.models_dir / "sherpa-onnx" / "speech-enhancement" / MODEL_NAME
    service = AudioEnhancementService(SherpaDpdfNetBackend(model))

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
