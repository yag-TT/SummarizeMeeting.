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
    monkeypatch.setattr(sherpa_runtime.sys, "platform", "linux")
    monkeypatch.setattr(
        sherpa_runtime,
        "_onnxruntime_capi_directory",
        lambda: capi_directory,
    )

    environment = sherpa_runtime.prepare_sherpa_onnx_environment(
        {
            "XDG_CACHE_HOME": str(cache_root),
            "LD_LIBRARY_PATH": "/existing",
        }
    )

    alias_directory = cache_root / "summarize-meeting" / "native-libs" / "onnxruntime"
    alias = alias_directory / "libonnxruntime.so"
    assert alias.is_symlink()
    assert alias.resolve() == current.resolve()
    assert environment["LD_LIBRARY_PATH"].split(os.pathsep) == [
        str(alias_directory),
        str(capi_directory),
        "/existing",
    ]


def test_non_linux_environment_is_unchanged(monkeypatch) -> None:
    monkeypatch.setattr(sherpa_runtime.sys, "platform", "win32")

    assert sherpa_runtime.prepare_sherpa_onnx_environment({"VALUE": "1"}) == {
        "VALUE": "1"
    }
