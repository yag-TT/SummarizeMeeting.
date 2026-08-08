from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from summarize_meeting.processing.transcription import (
    FasterWhisperBackend,
    TranscriptionService,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a local transcription job")
    parser.add_argument("--session", required=True, type=Path)
    parser.add_argument("--models-dir", required=True, type=Path)
    parser.add_argument("--cuda-runtime-dir", type=Path)
    parser.add_argument("--model", default="large-v3-turbo")
    parser.add_argument("--language", default="ja")
    args = parser.parse_args(argv)
    dll_directory_handle = _configure_cuda_runtime(args.cuda_runtime_dir)
    backend = FasterWhisperBackend(
        model_name=args.model,
        models_directory=args.models_dir / "faster-whisper",
    )
    service = TranscriptionService(backend, model_name=args.model)

    def progress(percent: int, message: str) -> None:
        print(
            json.dumps(
                {"type": "progress", "percent": percent, "message": message},
                ensure_ascii=False,
            ),
            flush=True,
        )

    try:
        output = service.run(
            args.session,
            language=args.language,
            progress_callback=progress,
        )
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1
    print(json.dumps({"type": "result", "path": str(output)}, ensure_ascii=False), flush=True)
    if dll_directory_handle is not None:
        dll_directory_handle.close()
    return 0


def _configure_cuda_runtime(directory: Path | None):
    if sys.platform != "win32" or directory is None or not directory.is_dir():
        return None
    resolved = str(directory.resolve())
    os.environ["PATH"] = resolved + os.pathsep + os.environ.get("PATH", "")
    return os.add_dll_directory(resolved)


if __name__ == "__main__":
    raise SystemExit(main())
