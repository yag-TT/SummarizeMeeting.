"""解析ワーカープロセスのOS別起動オプションとプロセスツリー終了処理。"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

import psutil


def platform_popen_options() -> dict[str, Any]:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def terminate_process_tree(process: subprocess.Popen[str], *, timeout: float = 1.0) -> None:
    """子や孫プロセスを残さず、猶予後も生存するプロセスは強制終了する。"""

    pid = getattr(process, "pid", None)
    if not isinstance(pid, int):
        process.terminate()
        return
    try:
        parent = psutil.Process(pid)
        processes = parent.children(recursive=True)
        processes.append(parent)
    except psutil.NoSuchProcess:
        return
    except psutil.AccessDenied:
        process.terminate()
        return
    for item in processes:
        try:
            item.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    _gone, alive = psutil.wait_procs(processes, timeout=timeout)
    for item in alive:
        try:
            item.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    if alive:
        psutil.wait_procs(alive, timeout=timeout)
