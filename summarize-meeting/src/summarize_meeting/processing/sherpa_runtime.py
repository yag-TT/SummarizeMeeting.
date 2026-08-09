from __future__ import annotations

import ctypes
import importlib.metadata
import importlib.util
import os
import platform
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

_CUDA_LIBRARIES = (
    "libcuda.so.1",
    "libcudart.so.12",
    "libcublas.so.12",
    "libcublasLt.so.12",
    "libcufft.so.11",
    "libcurand.so.10",
    "libcudnn.so.9",
)
_GPU_WHEEL_MARKERS = ("+cuda12", ".cudnn9")


@dataclass(frozen=True, slots=True)
class SherpaCudaStatus:
    available: bool
    targeted: bool
    wheel_version: str
    missing_libraries: tuple[str, ...] = ()
    reason: str = ""


def prepare_sherpa_onnx_environment(
    environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return an environment that can load sherpa-onnx on supported platforms."""
    prepared = dict(os.environ if environment is None else environment)
    if not sys.platform.startswith("linux"):
        return prepared

    sherpa_library_directory = _sherpa_onnx_library_directory()
    if sherpa_library_directory is not None:
        return _prepend_library_paths(prepared, sherpa_library_directory)

    capi_directory = _onnxruntime_capi_directory()
    runtime_library = _onnxruntime_library(capi_directory)
    alias_directory = _native_library_cache_directory(prepared)
    alias_directory.mkdir(parents=True, exist_ok=True)
    alias = alias_directory / "libonnxruntime.so"
    _ensure_library_alias(alias, runtime_library)

    return _prepend_library_paths(prepared, alias_directory, capi_directory)


def sherpa_cuda_status() -> SherpaCudaStatus:
    """Return whether the Linux x86_64 sherpa CUDA runtime is ready."""
    try:
        wheel_version = importlib.metadata.version("sherpa-onnx")
    except importlib.metadata.PackageNotFoundError:
        return SherpaCudaStatus(
            available=False,
            targeted=sys.platform.startswith("linux"),
            wheel_version="not installed",
            reason="sherpa-onnxがありません",
        )

    targeted = sys.platform.startswith("linux") and platform.machine().casefold() in {
        "x86_64",
        "amd64",
    }
    if not targeted:
        return SherpaCudaStatus(
            available=False,
            targeted=False,
            wheel_version=wheel_version,
            reason="CUDA話者分離の対象はLinux x86_64だけです",
        )
    if not all(marker in wheel_version.casefold() for marker in _GPU_WHEEL_MARKERS):
        return SherpaCudaStatus(
            available=False,
            targeted=True,
            wheel_version=wheel_version,
            reason=f"GPU版sherpa-onnxではありません: {wheel_version}",
        )

    missing = tuple(name for name in _CUDA_LIBRARIES if not _can_load_library(name))
    if missing:
        return SherpaCudaStatus(
            available=False,
            targeted=True,
            wheel_version=wheel_version,
            missing_libraries=missing,
            reason="CUDA共有ライブラリがありません: " + ", ".join(missing),
        )
    return SherpaCudaStatus(available=True, targeted=True, wheel_version=wheel_version)


def _sherpa_onnx_library_directory() -> Path | None:
    spec = importlib.util.find_spec("sherpa_onnx")
    if spec is None or spec.origin is None:
        return None
    library_directory = Path(spec.origin).resolve().parent / "lib"
    required = (
        library_directory / "libonnxruntime.so",
        library_directory / "libonnxruntime_providers_cuda.so",
    )
    return library_directory if all(path.is_file() for path in required) else None


def _prepend_library_paths(environment: dict[str, str], *paths: Path) -> dict[str, str]:
    existing = environment.get("LD_LIBRARY_PATH", "")
    search_paths = [str(path) for path in paths]
    search_paths.extend(value for value in existing.split(os.pathsep) if value)
    environment["LD_LIBRARY_PATH"] = os.pathsep.join(dict.fromkeys(search_paths))
    return environment


def _can_load_library(name: str) -> bool:
    try:
        ctypes.CDLL(name)
    except OSError:
        return False
    return True


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
