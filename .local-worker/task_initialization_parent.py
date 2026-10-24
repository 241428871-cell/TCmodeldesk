"""Keep the original rework slot and parent evidence across initialization retries."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import control_bootstrap
import delegation_policy
from pydantic import JsonValue, TypeAdapter

if TYPE_CHECKING:
    from model_desk_control.host_initialization_models import InitialDraft
    from model_desk_control.host_queue_models import Ticket

type Record = dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class ParentProof:
    """Only hashes leave the original parent records."""

    task: str
    review: str


class ReworkPolicy(Protocol):
    """The existing serialized policy remains the only writer of a rework claim."""

    def mark_rework_dispatched(
        self, run: Path, child_id: str, *, initialization: InitialDraft
    ) -> Record:
        """Revalidate the initialization proof under the original review lock."""
        ...


def policy() -> ReworkPolicy:
    """Keep the legacy policy boundary narrow without replacing its actual implementation."""
    return delegation_policy


def snapshot(ticket: Ticket, parent_id: str) -> ParentProof:
    """Bind parent identity and review contents, allowing only this child's own claim marker."""
    control_bootstrap.bind_source()
    from model_desk_control.host_initialization_models import InitializationError
    from model_desk_control.host_trace_models import RecordId
    from model_desk_control.native_output import owned_path, read_metadata

    identity = TypeAdapter(RecordId).validate_python(parent_id)
    relative = Path(".model-desk/runs") / identity
    task = TypeAdapter(Record).validate_json(
        read_metadata(owned_path(Path(ticket.project), relative / "task.json"))
    )
    review = TypeAdapter(Record).validate_json(
        read_metadata(owned_path(Path(ticket.project), relative / "review.json"))
    )
    if (task.get("id"), task.get("project"), task.get("goal_id"), task.get("stage_id")) != (
        identity,
        ticket.project,
        ticket.goal_id,
        ticket.stage_id,
    ):
        code = "initialization_parent_identity"
        raise InitializationError(code)
    rework = TypeAdapter(Record).validate_python(review.get("rework"))
    if rework.get("dispatched_task_id") not in (None, "", ticket.task_id):
        code = "initialization_rework_already_claimed"
        raise InitializationError(code)
    for key in ("dispatched_task_id", "dispatched_at"):
        _ = rework.pop(key, None)
    review["rework"] = rework
    _ = review.pop("updated_at", None)
    _ = task.pop("updated_at", None)
    return ParentProof(
        sha256(json.dumps(task, sort_keys=True).encode()).hexdigest(),
        sha256(json.dumps(review, sort_keys=True).encode()).hexdigest(),
    )


def verify(run: Path, draft: InitialDraft, child_id: str) -> None:
    """Called while the existing policy holds review.lock; it never takes that lock twice."""
    control_bootstrap.bind_source()
    from model_desk_control.host_initialization_models import InitializationError

    expected = Path(draft.ticket.project) / ".model-desk/runs" / draft.rework_parent_id
    proof = snapshot(draft.ticket, draft.rework_parent_id)
    if (
        run != expected
        or child_id != draft.ticket.task_id
        or proof != ParentProof(draft.parent_fingerprint, draft.review_fingerprint)
    ):
        code = "initialization_parent_evidence_changed"
        raise InitializationError(code)


def consume(draft: InitialDraft) -> None:
    """An already-claimed child is idempotent; no retry resets the parent's rework count."""
    if draft.rework_parent_id:
        run = Path(draft.ticket.project) / ".model-desk/runs" / draft.rework_parent_id
        _ = policy().mark_rework_dispatched(run, draft.ticket.task_id, initialization=draft)
