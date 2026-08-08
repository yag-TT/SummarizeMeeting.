from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from summarize_meeting.infrastructure.paths import PortableAppPaths
from summarize_meeting.processing.screen_analysis import paddle_models_status


@dataclass(frozen=True, slots=True)
class Check:
    level: str
    name: str
    detail: str


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose Summarize Meeting runtime")
    parser.add_argument("--app-root", type=Path)
    args = parser.parse_args()
    paths = (
        PortableAppPaths(args.app_root.resolve())
        if args.app_root
        else PortableAppPaths.discover()
    )
    checks = [
        _check_platform(),
        _check_write_access(paths),
        *_check_desktop_session(),
        _check_audio(),
        _check_ocr_models(paths),
        _check_cuda(),
    ]
    for check in checks:
        print(f"[{check.level}] {check.name}: {check.detail}")
    return 1 if any(check.level == "ERROR" for check in checks) else 0


def _check_platform() -> Check:
    supported = sys.version_info >= (3, 11) and platform.system() in {"Windows", "Linux"}
    return Check(
        "OK" if supported else "ERROR",
        "platform",
        f"{platform.system()} {platform.release()} / Python {platform.python_version()}",
    )


def _check_write_access(paths: PortableAppPaths) -> Check:
    try:
        paths.data_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=paths.data_dir, prefix="doctor-", delete=True):
            pass
    except OSError as exc:
        return Check("ERROR", "storage", f"{paths.data_dir}: {exc}")
    return Check("OK", "storage", str(paths.data_dir))


def _check_desktop_session() -> list[Check]:
    if platform.system() != "Linux":
        return [Check("OK", "screen-capture", "Qt Multimedia (Windows)")]
    session_type = os.environ.get("XDG_SESSION_TYPE", "unknown").casefold()
    display = os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")
    checks = [
        Check(
            "OK" if display else "ERROR",
            "desktop-session",
            f"type={session_type}, display={display or 'not found'}",
        )
    ]
    if os.environ.get("SSH_CONNECTION"):
        checks.append(Check("WARN", "remote-session", "SSHのみの実行は画面取得対象外です"))
    if session_type == "wayland":
        missing = [name for name in ("pipewire", "ffmpeg", "gdbus") if shutil.which(name) is None]
        checks.append(
            Check(
                "ERROR" if missing else "OK",
                "wayland-tools",
                "missing: " + ", ".join(missing) if missing else "PipeWire/Portal tools found",
            )
        )
        checks.append(
            _check_debian_packages(
                "wayland-packages",
                ("pipewire", "xdg-desktop-portal", "xdg-desktop-portal-gnome", "ffmpeg"),
            )
        )
        checks.append(_check_portal())
    elif session_type != "x11":
        checks.append(Check("WARN", "screen-capture", "Wayland/X11を判定できません"))
    return checks


def _check_debian_packages(name: str, packages: tuple[str, ...]) -> Check:
    if shutil.which("dpkg-query") is None:
        return Check("WARN", name, "dpkg-queryがないため確認できません")
    missing: list[str] = []
    for package in packages:
        try:
            result = subprocess.run(
                ["dpkg-query", "-W", "-f=${db:Status-Abbrev}", package],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            missing.append(package)
            continue
        if result.returncode != 0 or not result.stdout.startswith("ii "):
            missing.append(package)
    return Check(
        "ERROR" if missing else "OK",
        name,
        "missing: " + ", ".join(missing) if missing else "required packages installed",
    )


def _check_portal() -> Check:
    if shutil.which("gdbus") is None:
        return Check("ERROR", "screen-cast-portal", "gdbusがありません")
    try:
        result = subprocess.run(
            [
                "gdbus",
                "call",
                "--session",
                "--dest",
                "org.freedesktop.portal.Desktop",
                "--object-path",
                "/org/freedesktop/portal/desktop",
                "--method",
                "org.freedesktop.DBus.Properties.Get",
                "org.freedesktop.portal.ScreenCast",
                "version",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check("ERROR", "screen-cast-portal", str(exc))
    detail = result.stdout.strip() or result.stderr.strip()
    return Check("OK" if result.returncode == 0 else "ERROR", "screen-cast-portal", detail)


def _check_audio() -> Check:
    try:
        import soundcard as sc

        microphones = sc.all_microphones(include_loopback=False)
        loopbacks = [
            item for item in sc.all_microphones(include_loopback=True) if item.isloopback
        ]
    except Exception as exc:
        return Check("ERROR", "audio", str(exc))
    server = _audio_server_name()
    return Check(
        "OK" if microphones or loopbacks else "WARN",
        "audio",
        f"server={server}, microphones={len(microphones)}, loopbacks={len(loopbacks)}",
    )


def _audio_server_name() -> str:
    if platform.system() != "Linux" or shutil.which("pactl") is None:
        return "WASAPI" if platform.system() == "Windows" else "unknown"
    try:
        result = subprocess.run(
            ["pactl", "info"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    for line in result.stdout.splitlines():
        if line.casefold().startswith("server name:"):
            return line.split(":", 1)[1].strip()
    return "PulseAudio-compatible" if result.returncode == 0 else "unavailable"


def _check_ocr_models(paths: PortableAppPaths) -> Check:
    status = paddle_models_status(paths.models_dir / "paddleocr")
    missing = [name for name, available in status.items() if not available]
    return Check(
        "WARN" if missing else "OK",
        "paddleocr-models",
        "missing: " + ", ".join(missing) if missing else "verified",
    )


def _check_cuda() -> Check:
    try:
        import ctranslate2

        count = ctranslate2.get_cuda_device_count()
    except Exception as exc:
        return Check("WARN", "cuda", f"利用不可（CPUを使用）: {exc}")
    return Check(
        "OK" if count > 0 else "WARN",
        "cuda",
        f"devices={count}" if count > 0 else "GPUなし（CPUを使用）",
    )


if __name__ == "__main__":
    raise SystemExit(main())
