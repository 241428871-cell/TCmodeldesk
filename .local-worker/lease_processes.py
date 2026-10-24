"""Conservative local process/session evidence, independent of task completion labels."""

import os
from typing import Final

import lease_descendants
import psutil

from model_desk_control.durable_lease_models import DurableLease, LeasePhase
from model_desk_control.host_queue_models import Process

TOLERANCE: Final = 0.01


def exited(process: Process) -> bool:
    """Only an exact exit, zombie or PID reuse is an affirmative exit observation."""
    if process.birth is None:
        return False
    try:
        observed = psutil.Process(process.pid)
        return (
            abs(observed.create_time() - process.birth) > TOLERANCE
            or observed.status() == psutil.STATUS_ZOMBIE
        )
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return True
    except psutil.Error:
        return False


def quiet(receipt: DurableLease) -> bool:
    """Check the session plus durably observed descendants, even after setsid.

    Managed subprocesses inherit this session. This is not an OS sandbox against
    Sampling can miss a short-lived intermediate parent; this is not a complete
    kernel fork trace or OS sandbox. Incomplete observation never passes.
    """
    if (
        receipt.phase != LeasePhase.STARTED
        or receipt.launcher is None
        or os.name == "nt"
        or receipt.containment != "posix_session"
    ):
        return False
    if not exited(receipt.launcher):
        return False
    try:
        for pid in psutil.pids():
            if pid <= 0:
                continue
            try:
                if os.getsid(pid) == receipt.launcher.pid:
                    observed = psutil.Process(pid)
                    if observed.status() != psutil.STATUS_ZOMBIE:
                        return False
            except (ProcessLookupError, psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
    except (OSError, psutil.Error):
        return False
    return lease_descendants.drained(receipt, exited)
