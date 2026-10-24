"""Process supervision helpers for the detached API and worker processes.

Windows notes:
- Detached children are spawned with DETACHED_PROCESS so they survive the
  launching terminal (spec 2.1: closing a terminal must not stop the core).
- PID files record the process creation time; a reused PID that belongs to a
  different process is rejected rather than signalled.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil


def write_pid_file(path: Path, pid: int, role: str) -> dict[str, Any]:
    create_time: float | None = None
    try:
        create_time = psutil.Process(pid).create_time()
    except psutil.Error:
        pass
    record = {
        "pid": pid,
        "role": role,
        "started_at": datetime.now(UTC).isoformat(),
        "create_time": create_time,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return record


def read_pid_file(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    return data if isinstance(data, dict) else None


def process_alive(record: dict[str, Any] | None) -> bool:
    """True only if the recorded PID is live AND is the same process we started."""
    if not record:
        return False
    pid = record.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        proc = psutil.Process(pid)
    except psutil.Error:
        return False
    if proc.status() == psutil.STATUS_ZOMBIE:
        return False
    recorded_create = record.get("create_time")
    if isinstance(recorded_create, (int, float)):
        try:
            if abs(proc.create_time() - recorded_create) > 2.0:
                return False  # PID was reused by an unrelated process
        except psutil.Error:
            return False
    return True


def spawn_detached(module: str, log_path: Path, env: dict[str, str], cwd: Path) -> int:
    """Start ``python -m <module>`` fully detached, appending output to log_path.

    On Windows the child first attempts ``CREATE_BREAKAWAY_FROM_JOB`` so it
    survives process-tree cleanup by the launching terminal or harness; if the
    parent job forbids breakaway, spawning is retried without the flag.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "ab")
    try:
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            base_flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
            kwargs["creationflags"] = base_flags | breakaway
        else:  # pragma: no cover - POSIX development path
            kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", module],
                stdout=log_file,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                cwd=str(cwd),
                env=env,
                close_fds=True,
                **kwargs,
            )
        except OSError:
            if os.name != "nt" or not kwargs.get("creationflags"):
                raise
            # Parent job does not allow breakaway: retry with plain detachment.
            kwargs["creationflags"] = (
                subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            )
            proc = subprocess.Popen(
                [sys.executable, "-m", module],
                stdout=log_file,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                cwd=str(cwd),
                env=env,
                close_fds=True,
                **kwargs,
            )
    finally:
        log_file.close()
    return proc.pid


def log_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def wait_for_log_line(path: Path, needle: str, timeout: float, start_offset: int = 0) -> bool:
    """Wait until needle appears in bytes appended after start_offset."""
    import time

    deadline = time.monotonic() + timeout
    target = needle.encode("utf-8")
    while time.monotonic() < deadline:
        try:
            with open(path, "rb") as handle:
                handle.seek(start_offset)
                if target in handle.read():
                    return True
        except OSError:
            pass
        time.sleep(0.2)
    return False


def terminate_process(record: dict[str, Any] | None, timeout: float = 5.0) -> bool:
    """Terminate a recorded process; returns True if a live process was stopped."""
    if not process_alive(record):
        return False
    assert record is not None
    proc = psutil.Process(int(record["pid"]))
    try:
        proc.terminate()
        proc.wait(timeout=timeout)
    except psutil.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=timeout)
        except psutil.TimeoutExpired:
            return False
    except psutil.Error:
        return False
    return True
