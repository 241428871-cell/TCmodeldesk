"""Immutable inputs for finishing the same admitted task after interrupted initialization."""

from enum import StrEnum
from typing import Final, Literal

from pydantic import Field

from model_desk_control.host_flow import HostRecord
from model_desk_control.host_queue_models import Ticket, ticket
from model_desk_control.host_trace_models import CaptureId, RecordId

MAX_INITIALIZATION: Final = 1024 * 1024


class InitializationError(ValueError):
    """The original admitted task cannot be safely materialized from these records."""

    def __init__(self, code: str) -> None:
        """Keep diagnostics independent of private task content."""
        self.code: str = code
        super().__init__(code)


class InitialPhase(StrEnum):
    """Ready means the original queue receipt committed, not that execution finished."""

    PENDING = "pending"
    READY = "ready"


class InitialKey(HostRecord):
    """The caller must identify every original dispatch dimension before recovery writes."""

    project: str
    goal_id: RecordId
    stage_id: RecordId
    round_id: RecordId
    request_id: RecordId
    epoch: int = Field(gt=0)
    payload_digest: str


class InitialDraft(HostRecord):
    """A private original input manifest; public audit records retain hashes only."""

    ticket: Ticket
    task_json: str
    prompt: str
    rework_parent_id: CaptureId = ""
    parent_fingerprint: str = ""
    review_fingerprint: str = ""

    def verify(self) -> None:
        """Check the entire saved input before treating a manifest as completion evidence."""
        if ticket(self.task_json, self.prompt) != self.ticket:
            code = "initialization_input_mismatch"
            raise InitializationError(code)
        if len(self.model_dump_json().encode()) > MAX_INITIALIZATION:
            code = "initialization_budget"
            raise InitializationError(code)
        if bool(self.rework_parent_id) != bool(self.parent_fingerprint and self.review_fingerprint):
            code = "initialization_parent_receipt_incomplete"
            raise InitializationError(code)

    def matches(self, key: InitialKey) -> bool:
        """Never let a receipt for one action repair another stage or authorization epoch."""
        return all(
            getattr(self.ticket, name) == getattr(key, name) for name in InitialKey.model_fields
        )


class InitialRecord(HostRecord):
    """One durable input manifest and its bounded initialization position."""

    version: Literal[1] = 1
    phase: InitialPhase = InitialPhase.PENDING
    draft: InitialDraft
