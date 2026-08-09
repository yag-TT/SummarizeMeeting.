from __future__ import annotations

import importlib.util
import json
import platform
import sys
from pathlib import Path

_DOCTOR_PATH = Path(__file__).resolve().parents[2] / "scripts" / "doctor.py"
_SPEC = importlib.util.spec_from_file_location("doctor_under_test", _DOCTOR_PATH)
assert _SPEC is not None and _SPEC.loader is not None
doctor = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = doctor
_SPEC.loader.exec_module(doctor)


def test_wslg_does_not_require_native_wayland_packages(monkeypatch) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setenv("WSL_INTEROP", "/run/WSL/1_interop")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("PULSE_SERVER", "unix:/mnt/wslg/PulseServer")
    monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)

    checks = doctor._check_desktop_session()

    assert [(check.level, check.name) for check in checks] == [
        ("OK", "desktop-session"),
        ("WARN", "screen-capture"),
    ]


def test_native_wayland_package_check_excludes_unused_tools(monkeypatch) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.delenv("WSL_INTEROP", raising=False)
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: "/usr/bin/tool")
    checked_packages: list[tuple[str, ...]] = []

    def check_packages(_name: str, packages: tuple[str, ...]) -> doctor.Check:
        checked_packages.append(packages)
        return doctor.Check("OK", "wayland-packages", "installed")

    monkeypatch.setattr(doctor, "_check_debian_packages", check_packages)
    monkeypatch.setattr(
        doctor,
        "_check_portal",
        lambda: doctor.Check("OK", "screen-cast-portal", "available"),
    )

    checks = doctor._check_desktop_session()

    assert all(check.level == "OK" for check in checks)
    assert checked_packages == [
        ("pipewire", "xdg-desktop-portal", "xdg-desktop-portal-gnome")
    ]


def test_speaker_diarization_runtime_uses_prepared_environment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    prepared = {"LD_LIBRARY_PATH": "/runtime"}
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        doctor,
        "prepare_sherpa_onnx_environment",
        lambda _environment: prepared,
    )

    class _Result:
        returncode = 0
        stderr = ""
        stdout = json.dumps(
            {
                "provider": "cuda",
                "wheel_version": "1.13.4+cuda12.cudnn9",
                "warnings": [],
            }
        )

    def run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return _Result()

    monkeypatch.setattr(doctor.subprocess, "run", run)

    paths = doctor.PortableAppPaths(tmp_path)
    check = doctor._check_speaker_diarization_runtime(paths)

    assert check.level == "OK"
    assert "provider=cuda" in check.detail
    assert captured["env"] is prepared
    assert "summarize_meeting.processing.diarization_probe" in captured["command"]
    assert str(paths.models_dir / "sherpa-onnx" / "diarization") in " ".join(
        captured["command"]
    )


def test_speaker_diarization_runtime_warns_for_cpu_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        doctor,
        "prepare_sherpa_onnx_environment",
        lambda environment: environment,
    )

    class _Result:
        returncode = 0
        stderr = ""
        stdout = json.dumps(
            {
                "provider": "cpu",
                "wheel_version": "1.13.4+cuda12.cudnn9",
                "warnings": ["CUDA共有ライブラリがありません: libcudnn.so.9"],
            }
        )

    monkeypatch.setattr(doctor.subprocess, "run", lambda *_args, **_kwargs: _Result())

    check = doctor._check_speaker_diarization_runtime(doctor.PortableAppPaths(tmp_path))

    assert check.level == "WARN"
    assert "provider=cpu" in check.detail
    assert "libcudnn.so.9" in check.detail


def test_speaker_diarization_runtime_errors_when_probe_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        doctor,
        "prepare_sherpa_onnx_environment",
        lambda environment: environment,
    )

    class _Result:
        returncode = 1
        stderr = ""
        stdout = json.dumps({"status": "ERROR", "error": "CPU initialization failed"})

    monkeypatch.setattr(doctor.subprocess, "run", lambda *_args, **_kwargs: _Result())

    check = doctor._check_speaker_diarization_runtime(doctor.PortableAppPaths(tmp_path))

    assert check.level == "ERROR"
    assert check.detail == "CPU initialization failed"
