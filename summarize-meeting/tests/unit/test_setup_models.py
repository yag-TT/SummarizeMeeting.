from __future__ import annotations

import importlib.util
import io
import sys
import tarfile
from pathlib import Path

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "setup_models.py"
_SPEC = importlib.util.spec_from_file_location("setup_models_under_test", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
setup_models = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = setup_models
_SPEC.loader.exec_module(setup_models)


def test_setup_diarization_installs_cpu_and_cuda_segmentation_models(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(setup_models, "PROJECT_ROOT", tmp_path)

    def download(url: str, destination: Path, _expected_sha256: str) -> None:
        if url == setup_models.EMBEDDING_URL:
            destination.write_bytes(b"embedding")
            return
        files = {
            "model.onnx": b"cuda-float",
            "model.int8.onnx": b"cpu-int8",
            "LICENSE": b"license",
            "README.md": b"readme",
        }
        with tarfile.open(destination, "w:bz2") as bundle:
            for name, value in files.items():
                member = tarfile.TarInfo(
                    "sherpa-onnx-pyannote-segmentation-3-0/" + name
                )
                member.size = len(value)
                bundle.addfile(member, io.BytesIO(value))

    monkeypatch.setattr(setup_models, "_download_verified", download)

    setup_models.setup_diarization(force=False)

    root = tmp_path / "models/sherpa-onnx/diarization"
    assert (root / "segmentation/model.onnx").read_bytes() == b"cuda-float"
    assert (root / "segmentation/model.int8.onnx").read_bytes() == b"cpu-int8"
    assert (root / "embedding/nemo_en_titanet_small.onnx").read_bytes() == b"embedding"
