from __future__ import annotations

import importlib.util
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path


def prepare_sherpa_onnx_environment(
    environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return an environment that can load sherpa-onnx on supported platforms."""
    prepared = dict(os.environ if environment is None else environment)
    if not sys.platform.startswith("linux"):
        return prepared

    capi_directory = _onnxruntime_capi_directory()
    runtime_library = _onnxruntime_library(capi_directory)
    alias_directory = _native_library_cache_directory(prepared)
    alias_directory.mkdir(parents=True, exist_ok=True)
    alias = alias_directory / "libonnxruntime.so"
    _ensure_library_alias(alias, runtime_library)

    existing = prepared.get("LD_LIBRARY_PATH", "")
    search_paths = [str(alias_directory), str(capi_directory)]
    search_paths.extend(value for value in existing.split(os.pathsep) if value)
    prepared["LD_LIBRARY_PATH"] = os.pathsep.join(dict.fromkeys(search_paths))
    return prepared


def _onnxruntime_capi_directory() -> Path:
    spec = importlib.util.find_spec("onnxruntime")
    if spec is None or spec.origin is None:
        raise RuntimeError("onnxruntimeパッケージがありません")
    capi_directory = Path(spec.origin).resolve().parent / "capi"
    if not capi_directory.is_dir():
        raise RuntimeError(f"ONNX Runtimeライブラリフォルダがありません: {capi_directory}")
    return capi_directory


def _onnxruntime_library(capi_directory: Path) -> Path:
    candidates = tuple(capi_directory.glob("libonnxruntime.so.*"))
    if not candidates:
        raise RuntimeError(f"ONNX Runtime共有ライブラリがありません: {capi_directory}")
    return max(candidates, key=_library_version)


def _library_version(path: Path) -> tuple[int, ...]:
    return tuple(int(value) for value in re.findall(r"\d+", path.name))


def _native_library_cache_directory(environment: Mapping[str, str]) -> Path:
    configured = environment.get("XDG_CACHE_HOME")
    if configured and Path(configured).is_absolute():
        cache_root = Path(configured)
    else:
        cache_root = Path.home() / ".cache"
    return cache_root / "summarize-meeting" / "native-libs" / "onnxruntime"


def _ensure_library_alias(alias: Path, target: Path) -> None:
    if alias.is_symlink() and alias.resolve() == target.resolve():
        return
    if alias.exists() and not alias.is_symlink():
        raise RuntimeError(f"共有ライブラリ用キャッシュを更新できません: {alias}")
    temporary = alias.with_name(f"{alias.name}.{os.getpid()}.tmp")
    temporary.unlink(missing_ok=True)
    try:
        temporary.symlink_to(target.resolve())
        os.replace(temporary, alias)
    finally:
        temporary.unlink(missing_ok=True)
