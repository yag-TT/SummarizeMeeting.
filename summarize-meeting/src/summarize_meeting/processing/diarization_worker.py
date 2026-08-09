from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from summarize_meeting.processing.sherpa_runtime import (
    prepare_sherpa_onnx_environment,
)

_RUNTIME_PREPARED_ENV = "SUMMARIZE_MEETING_SHERPA_RUNTIME_PREPARED"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a local speaker diarization job")
    parser.add_argument("--session", required=True, type=Path)
    parser.add_argument("--models-dir", required=True, type=Path)
    parser.add_argument("--speaker-count", type=int)
    parser.add_argument("--cluster-threshold", default=0.75, type=float)
    args = parser.parse_args(argv)
    _ensure_native_runtime_environment()
    from summarize_meeting.processing.diarization import (
        DiarizationService,
        SherpaOnnxDiarizationBackend,
    )

    root = args.models_dir / "sherpa-onnx" / "diarization"
    backend = SherpaOnnxDiarizationBackend(
        segmentation_model=root / "segmentation" / "model.int8.onnx",
        embedding_model=root / "embedding" / "nemo_en_titanet_small.onnx",
    )
    service = DiarizationService(backend, cluster_threshold=args.cluster_threshold)

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
            speaker_count=args.speaker_count,
            progress_callback=progress,
        )
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1
    print(json.dumps({"type": "result", "path": str(output)}, ensure_ascii=False), flush=True)
    return 0


def _ensure_native_runtime_environment() -> None:
    if os.environ.get(_RUNTIME_PREPARED_ENV) == "1":
        return
    prepared = prepare_sherpa_onnx_environment(os.environ)
    if prepared.get("LD_LIBRARY_PATH") == os.environ.get("LD_LIBRARY_PATH"):
        return
    prepared[_RUNTIME_PREPARED_ENV] = "1"
    os.execve(
        sys.executable,
        [
            sys.executable,
            "-m",
            "summarize_meeting.processing.diarization_worker",
            *sys.argv[1:],
        ],
        prepared,
    )


if __name__ == "__main__":
    raise SystemExit(main())
