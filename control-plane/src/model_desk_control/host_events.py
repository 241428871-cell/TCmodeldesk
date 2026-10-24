"""Durable source observations consumed only by the original continuation owner."""

from datetime import UTC, datetime
from hashlib import sha256
from sqlite3 import Connection
from typing import Final, Literal

from pydantic import AwareDatetime, Field

from model_desk_control.database import text_cursor
from model_desk_control.host_flow import HostRecord
from model_desk_control.host_trace_models import LegacyObservation, RecordId

MAX_BATCH: Final = 64
MAX_EVENT_BYTES: Final = 4096


class EventError(ValueError):
    """Refuse a foreign or altered event without replaying a model call."""

    def __init__(self, code: str) -> None:
        """Retain a stable diagnostic, without copying task content."""
        self.code: str = code
        super().__init__(code)


class EventClock(LegacyObservation):
    """A source observation never creates or enables its goal clock."""

    project: str
    goal_id: RecordId
    authority_epoch: int = Field(ge=1)
    enabled: bool
    user_intent: str
    closed: bool
    active_intent_id: str
    event_not_before: AwareDatetime | None = None


class GoalEvent(HostRecord):
    """Only compact evidence identities enter the event inbox."""

    version: Literal[1] = 1
    project: str
    goal_id: RecordId
    authority_epoch: int = Field(ge=1)
    kind: Literal["task_observed", "human_answered"]
    subject_id: RecordId
    stage_id: str
    outcome: str
    evidence_digest: str = Field(pattern=r"^[a-f0-9]{64}$")

    @property
    def identity(self) -> str:
        """Redelivery of the same observation retains one durable identity."""
        return "event_" + sha256(self.model_dump_json().encode()).hexdigest()


def exists(database: Connection, table: str) -> bool:
    """Schema inspection has no mutation or service side effect."""
    return (
        text_cursor(
            database, "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def clock(database: Connection, project: str, goal_id: str) -> EventClock | None:
    """Require the actual project-owned goal and its existing clock."""
    if not exists(database, "continuation_clocks"):
        return None
    row = text_cursor(
        database,
        "SELECT substr(data,1,65537) FROM continuation_clocks WHERE goal_id=?",
        (goal_id,),
    ).fetchone()
    if row is None:
        return None
    saved = EventClock.model_validate_json(row[0])
    goal = text_cursor(database, "SELECT project FROM goals WHERE id=?", (goal_id,)).fetchone()
    if saved.project != project or saved.goal_id != goal_id or goal is None or goal[0] != project:
        code = "event_goal_identity"
        raise EventError(code)
    return saved


def record(database: Connection, event: GoalEvent, *, caused_by: str = "") -> None:
    """Commit a source event in its original mutation, never in a separate database."""
    if not database.in_transaction:
        code = "event_source_transaction_required"
        raise EventError(code)
    state = clock(database, event.project, event.goal_id)
    if state is None:
        code = "event_clock_missing"
        raise EventError(code)
    if caused_by and (
        state.active_intent_id != caused_by or state.authority_epoch != event.authority_epoch
    ):
        code = "event_cause_identity"
        raise EventError(code)
    raw = event.model_dump_json()
    if len(raw.encode()) > MAX_EVENT_BYTES:
        code = "event_record_budget"
        raise EventError(code)
    _ = database.execute(
        """CREATE TABLE IF NOT EXISTS goal_events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
        goal_id TEXT NOT NULL, epoch INTEGER NOT NULL, data TEXT NOT NULL CHECK(json_valid(data)),
        claimed_by TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL)"""
    )
    _ = database.execute(
        """CREATE INDEX IF NOT EXISTS goal_events_pending
        ON goal_events(goal_id,epoch,claimed_by,seq)"""
    )
    _ = database.execute(
        """INSERT INTO goal_events(id,goal_id,epoch,data,claimed_by,created_at)
        VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING""",
        (
            event.identity,
            event.goal_id,
            event.authority_epoch,
            raw,
            caused_by,
            datetime.now(UTC).isoformat(),
        ),
    )
    saved = text_cursor(
        database, "SELECT data FROM goal_events WHERE id=?", (event.identity,)
    ).fetchone()
    if saved is None or saved[0] != raw:
        code = "event_identity_conflict"
        raise EventError(code)


def pending(database: Connection, project: str, goal_id: str, epoch: int) -> tuple[GoalEvent, ...]:
    """Bounded read; old epochs and previously owned events cannot trigger another round."""
    if not exists(database, "goal_events"):
        return ()
    rows = text_cursor(
        database,
        """SELECT id,substr(data,1,4097) FROM goal_events
        WHERE goal_id=? AND epoch=? AND claimed_by='' ORDER BY seq LIMIT ?""",
        (goal_id, epoch, MAX_BATCH),
    ).fetchall()
    events: list[GoalEvent] = []
    for identity, raw in rows:
        if len(raw.encode()) > MAX_EVENT_BYTES:
            code = "event_record_budget"
            raise EventError(code)
        event = GoalEvent.model_validate_json(raw)
        if (
            event.identity != identity
            or event.project != project
            or event.goal_id != goal_id
            or event.authority_epoch != epoch
        ):
            code = "event_record_identity"
            raise EventError(code)
        events.append(event)
    return tuple(events)


def bind(database: Connection, project: str, goal_id: str, epoch: int, intent_id: str) -> list[str]:
    """The original claim transaction owns each observation once, alongside its round."""
    if not database.in_transaction:
        code = "event_claim_transaction_required"
        raise EventError(code)
    identities: list[str] = []
    for event in pending(database, project, goal_id, epoch):
        updated = database.execute(
            "UPDATE goal_events SET claimed_by=? WHERE id=? AND claimed_by=''",
            (intent_id, event.identity),
        )
        if updated.rowcount != 1:
            code = "event_claim_conflict"
            raise EventError(code)
        identities.append(event.identity)
    return identities
