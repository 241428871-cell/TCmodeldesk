"""Fenced delivery transitions; armed or started effects are never automatically repeated."""

from sqlite3 import Connection

from model_desk_control.host_queue_models import (
    Delivery,
    Owner,
    Phase,
    Process,
    QueueError,
    QueueState,
    Ticket,
)
from model_desk_control.host_queue_store import origin, read, write


def require(database: Connection, ticket: Ticket) -> QueueState:
    """The live task cannot replace its immutable admission snapshot."""
    state = read(database, ticket.task_id)
    if state is None or state.ticket != ticket:
        code = "queue_receipt_missing_or_input_changed"
        raise QueueError(code)
    origin(database, ticket)
    return state


def reserve(
    database: Connection,
    ticket: Ticket,
    delivery: Delivery,
    *,
    exited_owner: Owner | None = None,
    resume: bool = False,
) -> QueueState | None:
    """A new token fences old callbacks; only positive original-owner exit permits redelivery."""
    state = require(database, ticket)
    allowed = state.phase == Phase.READY or (resume and state.phase == Phase.BLOCKED)
    if state.phase in {Phase.QUEUED, Phase.CLAIMED}:
        allowed = state.owner is not None and state.owner == exited_owner
    if not allowed:
        return None
    result = QueueState(
        ticket=ticket, phase=Phase.QUEUED, delivery_id=delivery.identity, owner=delivery.owner
    )
    write(database, result, state)
    return result


def advance(
    database: Connection,
    task_id: str,
    delivery_id: str,
    phase: Phase,
    *,
    launcher: Process | None = None,
) -> QueueState | None:
    """Rejected stale callbacks cannot overwrite results produced by another token."""
    state = read(database, task_id)
    if state is None or state.delivery_id != delivery_id or not delivery_id:
        return None
    transitions = {
        Phase.READY: {Phase.QUEUED},
        Phase.CLAIMED: {Phase.QUEUED},
        Phase.ARMED: {Phase.CLAIMED},
        Phase.STARTED: {Phase.ARMED},
        Phase.DONE: {Phase.STARTED},
        Phase.BLOCKED: {Phase.CLAIMED, Phase.ARMED},
        Phase.UNKNOWN: {Phase.ARMED, Phase.STARTED},
    }
    if state.phase not in transitions.get(phase, set()):
        return None
    result = QueueState(
        ticket=state.ticket,
        phase=phase,
        delivery_id="" if phase == Phase.READY else delivery_id,
        owner=None if phase == Phase.READY else state.owner,
        acknowledged=False if phase == Phase.READY else state.acknowledged,
        launcher=launcher if launcher is not None else state.launcher,
        reason=state.reason,
    )
    write(database, result, state)
    return result


def acknowledge(database: Connection, task_id: str, delivery_id: str) -> None:
    """An enqueue reply cannot roll an already-running worker back to queued."""
    state = read(database, task_id)
    if state is None or state.delivery_id != delivery_id:
        code = "queue_ack_identity"
        raise QueueError(code)
    if state.acknowledged:
        return
    result = QueueState(
        ticket=state.ticket,
        phase=state.phase,
        delivery_id=delivery_id,
        owner=state.owner,
        acknowledged=True,
        launcher=state.launcher,
        reason=state.reason,
    )
    write(database, result, state)


def retire(
    database: Connection, expected: QueueState, phase: Phase, reason: str
) -> QueueState | None:
    """Fence a proven unstarted task without changing any armed or running invocation."""
    current = read(database, expected.ticket.task_id)
    if (
        current is None
        or current != expected
        or current.phase not in {Phase.READY, Phase.QUEUED, Phase.CLAIMED}
        or phase not in {Phase.BLOCKED, Phase.CANCELLED}
    ):
        return None
    result = QueueState(
        ticket=current.ticket,
        phase=phase,
        delivery_id=current.delivery_id,
        owner=current.owner,
        acknowledged=current.acknowledged,
        launcher=current.launcher,
        reason=reason,
    )
    write(database, result, current)
    return result
