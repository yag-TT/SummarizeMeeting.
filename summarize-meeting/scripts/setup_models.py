from __future__ import annotations

import argparse
import hashlib
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

SEGMENTATION_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
)
SEGMENTATION_SHA256 = "24615EE884C897D9D2BA09BB4D30DA6BB1B15E685065962DB5B02E76E4996488"
EMBEDDING_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "speaker-recongition-models/nemo_en_titanet_small.onnx"
)
EMBEDDING_SHA256 = "AD4A1802485D8B34C722D2A9D04249662F2ECE5D28A7A039063CA22F515A789E"
ENHANCEMENT_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "speech-enhancement-models/dpdfnet2_48khz_hr.onnx"
)
ENHANCEMENT_SHA256 = "0B399F8A58DC4D70D8CD97541F5C39869406145193B957D00A03B66070944928"


def main() -> int:
    parser = argparse.ArgumentParser(description="Download verified local inference models")
    parser.add_argument(
        "model",
        choices=("diarization", "audio-enhancement", "ocr", "all"),
    )
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if args.model in {"diarization", "all"}:
        setup_diarization(force=args.force)
    if args.model in {"audio-enhancement", "all"}:
        setup_audio_enhancement(force=args.force)
    if args.model in {"ocr", "all"}:
        setup_ocr(force=args.force)
    return 0


def setup_diarization(*, force: bool) -> None:
    model_root = PROJECT_ROOT / "models" / "sherpa-onnx" / "diarization"
    segmentation = model_root / "segmentation"
    embedding = model_root / "embedding"
    required = (
        segmentation / "model.int8.onnx",
        segmentation / "LICENSE",
        segmentation / "README.md",
        embedding / "nemo_en_titanet_small.onnx",
    )
    if not force and all(path.is_file() and path.stat().st_size > 0 for path in required):
        print(f"Speaker diarization models are already available: {model_root}")
        return

    model_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="setup-", dir=model_root) as temporary_value:
        temporary = Path(temporary_value)
        archive = temporary / "segmentation.tar.bz2"
        embedding_download = temporary / "nemo_en_titanet_small.onnx"
        _download_verified(SEGMENTATION_URL, archive, SEGMENTATION_SHA256)
        _download_verified(EMBEDDING_URL, embedding_download, EMBEDDING_SHA256)
        extract_root = temporary / "extract"
        extract_root.mkdir()
        with tarfile.open(archive, "r:bz2") as bundle:
            _extract_safely(bundle, extract_root)
        source = extract_root / "sherpa-onnx-pyannote-segmentation-3-0"
        segmentation.mkdir(parents=True, exist_ok=True)
        embedding.mkdir(parents=True, exist_ok=True)
        _copy_atomic(source / "model.int8.onnx", segmentation / "model.int8.onnx")
        _copy_atomic(source / "LICENSE", segmentation / "LICENSE")
        _copy_atomic(source / "README.md", segmentation / "README.md")
        _copy_atomic(embedding_download, embedding / "nemo_en_titanet_small.onnx")
    if not all(path.is_file() and path.stat().st_size > 0 for path in required):
        raise RuntimeError("Model setup completed but required files are missing")
    print(f"Speaker diarization models are ready: {model_root}")


def setup_audio_enhancement(*, force: bool) -> None:
    model_path = (
        PROJECT_ROOT
        / "models"
        / "sherpa-onnx"
        / "speech-enhancement"
        / "dpdfnet2_48khz_hr.onnx"
    )
    if not force and _matches_hash(model_path, ENHANCEMENT_SHA256):
        print(f"Audio enhancement model is already available: {model_path}")
        return
    model_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="setup-", dir=model_path.parent) as temporary_value:
        download = Path(temporary_value) / model_path.name
        _download_verified(ENHANCEMENT_URL, download, ENHANCEMENT_SHA256)
        _copy_atomic(download, model_path)
    if not _matches_hash(model_path, ENHANCEMENT_SHA256):
        raise RuntimeError(f"Model setup completed but the model is invalid: {model_path}")
    print(f"Audio enhancement model is ready: {model_path}")


def setup_ocr(*, force: bool) -> None:
    from summarize_meeting.processing.screen_analysis import ensure_paddle_models

    root = PROJECT_ROOT / "models" / "paddleocr"
    models = ensure_paddle_models(root, force=force)
    print("PaddleOCR models are ready:")
    for path in models.values():
        print(f"- {path}")


def _download_verified(url: str, destination: Path, expected_sha256: str) -> None:
    print(f"Downloading {url}")
    with urllib.request.urlopen(url) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output)
    if not _matches_hash(destination, expected_sha256):
        actual = _sha256(destination)
        raise RuntimeError(
            f"SHA-256 mismatch: {destination} expected={expected_sha256} actual={actual}"
        )


def _copy_atomic(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise RuntimeError(f"Downloaded archive is missing required file: {source.name}")
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    shutil.copyfile(source, temporary)
    temporary.replace(destination)


def _extract_safely(bundle: tarfile.TarFile, destination: Path) -> None:
    resolved_destination = destination.resolve()
    for member in bundle.getmembers():
        resolved_member = (destination / member.name).resolve()
        if (
            resolved_member != resolved_destination
            and resolved_destination not in resolved_member.parents
        ):
            raise RuntimeError(f"Archive member escapes extraction directory: {member.name}")
        if member.issym() or member.islnk():
            raise RuntimeError(f"Archive contains an unsupported link: {member.name}")
    bundle.extractall(destination)


def _matches_hash(path: Path, expected_sha256: str) -> bool:
    return path.is_file() and _sha256(path).casefold() == expected_sha256.casefold()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
