from __future__ import annotations

import argparse
import json
from pathlib import Path

from summarize_meeting.processing.diarization import SherpaOnnxDiarizationBackend
from summarize_meeting.processing.sherpa_runtime import sherpa_cuda_status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe the sherpa-onnx diarization runtime")
    parser.add_argument("--segmentation-model", required=True, type=Path)
    parser.add_argument("--embedding-model", required=True, type=Path)
    args = parser.parse_args(argv)

    status = sherpa_cuda_status()
    backend = SherpaOnnxDiarizationBackend(
        segmentation_model=args.segmentation_model,
        embedding_model=args.embedding_model,
    )
    try:
        provider = backend.probe_runtime()
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "error": f"{type(exc).__name__}: {exc}",
                    "wheel_version": status.wheel_version,
                    "cuda_ready": status.available,
                    "missing_libraries": status.missing_libraries,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 1
    print(
        json.dumps(
            {
                "status": "OK",
                "provider": provider,
                "warnings": backend.warnings,
                "wheel_version": status.wheel_version,
                "cuda_ready": status.available,
                "missing_libraries": status.missing_libraries,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
