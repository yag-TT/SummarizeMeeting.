from __future__ import annotations

import os
from pathlib import Path

from summarize_meeting.processing import sherpa_runtime


def test_linux_environment_adds_onnxruntime_library_alias(
    tmp_path: Path,
    monkeypatch,
) -> None:
    capi_directory = tmp_path / "site-packages" / "onnxruntime" / "capi"
    capi_directory.mkdir(parents=True)
    older = capi_directory / "libonnxruntime.so.1.9.0"
    current = capi_directory / "libonnxruntime.so.1.24.4"
    older.write_bytes(b"older")
    current.write_bytes(b"current")
    cache_root = tmp_path / "cache"
    aliases: list[tuple[Path, Path]] = []
    monkeypatch.setattr(sherpa_runtime.sys, "platform", "linux")
    monkeypatch.setattr(sherpa_runtime, "_sherpa_onnx_library_directory", lambda: None)
    monkeypatch.setattr(
        sherpa_runtime,
        "_onnxruntime_capi_directory",
        lambda: capi_directory,
    )
    monkeypatch.setattr(
        sherpa_runtime,
        "_ensure_library_alias",
        lambda alias, target: aliases.append((alias, target)),
    )

    environment = sherpa_runtime.prepare_sherpa_onnx_environment(
        {
            "XDG_CACHE_HOME": str(cache_root),
            "LD_LIBRARY_PATH": "/existing",
        }
    )

    alias_directory = cache_root / "summarize-meeting" / "native-libs" / "onnxruntime"
    alias = alias_directory / "libonnxruntime.so"
    assert aliases == [(alias, current)]
    assert environment["LD_LIBRARY_PATH"].split(os.pathsep) == [
        str(alias_directory),
        str(capi_directory),
        "/existing",
    ]


def test_linux_environment_prefers_gpu_wheel_runtime(
    tmp_path: Path,
    monkeypatch,
) -> None:
    library_directory = tmp_path / "site-packages" / "sherpa_onnx" / "lib"
    library_directory.mkdir(parents=True)
    (library_directory / "libonnxruntime.so").write_bytes(b"runtime")
    (library_directory / "libonnxruntime_providers_cuda.so").write_bytes(b"cuda")
    monkeypatch.setattr(sherpa_runtime.sys, "platform", "linux")
    monkeypatch.setattr(
        sherpa_runtime,
        "_sherpa_onnx_library_directory",
        lambda: library_directory,
    )

    environment = sherpa_runtime.prepare_sherpa_onnx_environment(
        {"LD_LIBRARY_PATH": "/external-cpu-runtime"}
    )

    assert environment["LD_LIBRARY_PATH"].split(os.pathsep) == [
        str(library_directory),
        "/external-cpu-runtime",
    ]


def test_cuda_status_reports_missing_runtime_libraries(monkeypatch) -> None:
    monkeypatch.setattr(sherpa_runtime.sys, "platform", "linux")
    monkeypatch.setattr(sherpa_runtime.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        sherpa_runtime.importlib.metadata,
        "version",
        lambda _name: "1.13.4+cuda12.cudnn9",
    )
    monkeypatch.setattr(
        sherpa_runtime,
        "_can_load_library",
        lambda name: name not in {"libcudart.so.12", "libcudnn.so.9"},
    )

    status = sherpa_runtime.sherpa_cuda_status()

    assert not status.available
    assert status.targeted
    assert status.missing_libraries == ("libcudart.so.12", "libcudnn.so.9")
    assert "libcudnn.so.9" in status.reason


def test_non_linux_environment_is_unchanged(monkeypatch) -> None:
    monkeypatch.setattr(sherpa_runtime.sys, "platform", "win32")

    assert sherpa_runtime.prepare_sherpa_onnx_environment({"VALUE": "1"}) == {
        "VALUE": "1"
    }
