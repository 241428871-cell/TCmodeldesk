"""Small platform-specific paths and process controls for local workers."""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import psutil


def process_birth(pid: int) -> float | None:
    """Read the exact process creation time; absence never becomes a guessed identity."""
    try:
        return psutil.Process(pid).create_time()
    except psutil.Error:
        return None


def worker_python(here: Path) -> Path:
    candidate = here / 'venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    return candidate if candidate.is_file() else Path(sys.executable)


def process_options() -> dict[str, int | bool]:
    # Managed workers retain one POSIX session so reparented children remain visible.
    if os.name != 'nt' and os.environ.get('AAD_LEASE_SESSION') == str(os.getsid(0)):
        return {'start_new_session': False}
    if os.name == 'nt':
        return {'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {'start_new_session': True}


def stop_tree(pid: int) -> None:
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'], capture_output=True, check=False)
        return
    managed = os.environ.get('AAD_LEASE_SESSION')
    if managed and managed == str(os.getsid(0)):
        # A nested CLI now shares the worker's session, so killpg(child_pid)
        # would miss it. Stop this lease's descendants while retaining the worker
        # long enough to save its cancellation/failure receipt.
        for child_pid in psutil.pids():
            if child_pid <= 0 or child_pid == os.getpid():
                continue
            try:
                if os.getsid(child_pid) == int(managed):
                    psutil.Process(child_pid).kill()
            except (ProcessLookupError, psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
