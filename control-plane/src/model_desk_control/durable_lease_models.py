"""Persisted resource ownership; elapsed time never proves a worker has stopped."""

from enum import StrEnum
from math import isfinite
from typing import Literal, Self

from pydantic import model_validator

from model_desk_control.host_flow import HostRecord
from model_desk_control.host_queue_models import Owner, Process
from model_desk_control.host_trace_models import RecordId
from model_desk_control.resource_leases import LeaseRequest


class LeaseStoreError(ValueError):
    """Stable refusal code, with no task prompts or credentials."""

    def __init__(self, code: str) -> None:
        """Expose a machine-readable refusal to the host adapter."""
        self.code: str = code
        super().__init__(code)


class LeasePhase(StrEnum):
    """Arming closes automatic reclamation even before the launcher receipt exists."""

    RESERVED = "reserved"
    ARMED = "armed"
    STARTED = "started"
    RELEASED = "released"


def require_process(process: Process) -> None:
    """A reused PID or an unreadable birth time cannot establish identity."""
    if process.birth is None or not isfinite(process.birth) or process.birth <= 0:
        code = "lease_process_identity_required"
        raise LeaseStoreError(code)


class DurableLease(HostRecord):
    """One immutable generation of a task's resource reservation."""

    token: RecordId
    request: LeaseRequest
    holder: Owner
    phase: LeasePhase = LeasePhase.RESERVED
    launcher: Process | None = None
    containment: Literal["none", "posix_session"] = "none"

    @model_validator(mode="after")
    def identities(self) -> Self:
        """Reject partial process identities and impossible persisted launch phases."""
        require_process(self.holder.process)
        if self.launcher is not None:
            require_process(self.launcher)
        if (self.phase == LeasePhase.STARTED and self.launcher is None) or (
            self.phase in (LeasePhase.RESERVED, LeasePhase.ARMED) and self.launcher is not None
        ):
            code = "lease_phase_identity_mismatch"
            raise LeaseStoreError(code)
        return self
