"""Deliver the exact observations owned by a native round, without claiming new events."""

from sqlite3 import Connection
from typing import Literal

from pydantic import Field

from model_desk_control.database import text_cursor
from model_desk_control.host_events import MAX_BATCH, MAX_EVENT_BYTES, EventError, GoalEvent
from model_desk_control.host_flow import HostRecord
from model_desk_control.host_trace_models import LegacyObservation, RecordId


class EventClaim(LegacyObservation):
    """Legacy rounds have no event grant; new claims carry an immutable list."""

    id: RecordId
    goal_id: RecordId
    authority_epoch: int
    trigger_kind: Literal["clock", "event"] = "clock"
    event_ids: tuple[RecordId, ...] = Field(default=(), max_length=MAX_BATCH)


class EventTrigger(HostRecord):
    """Task and decision references explain this real wakeup to the main brain."""

    kind: Literal["clock", "event"]
    operation_id: RecordId
    events: tuple[GoalEvent, ...]


def context(database: Connection, project: str, goal_id: str, claim_json: str) -> EventTrigger:
    """A snapshot cannot silently include another operation's event receipts."""
    claim = EventClaim.model_validate_json(claim_json)
    if claim.goal_id != goal_id or len(set(claim.event_ids)) != len(claim.event_ids):
        code = "event_context_claim_identity"
        raise EventError(code)
    if claim.trigger_kind == "event" and not claim.event_ids:
        code = "event_context_empty_trigger"
        raise EventError(code)
    events: list[GoalEvent] = []
    for identity in claim.event_ids:
        row = text_cursor(
            database,
            """SELECT substr(data,1,4097) FROM goal_events
            WHERE id=? AND goal_id=? AND epoch=? AND claimed_by=?""",
            (identity, goal_id, claim.authority_epoch, claim.id),
        ).fetchone()
        if row is None or len(row[0].encode()) > MAX_EVENT_BYTES:
            code = "event_context_receipt_missing"
            raise EventError(code)
        event = GoalEvent.model_validate_json(row[0])
        if (
            event.identity != identity
            or event.project != project
            or event.goal_id != goal_id
            or event.authority_epoch != claim.authority_epoch
        ):
            code = "event_context_receipt_identity"
            raise EventError(code)
        events.append(event)
    return EventTrigger(kind=claim.trigger_kind, operation_id=claim.id, events=tuple(events))
