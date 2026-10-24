"""Adopt only the original receipt-bearing action phase, never another native call."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import TYPE_CHECKING, ClassVar, Final, Literal

import continuation_store
import goal_brain_runner
from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter

from model_desk_control.database import text_cursor
from model_desk_control.host_flow import HostApplied, HostNativeResult, HostRef, HostSnapshot

if TYPE_CHECKING:
    from sqlite3 import Connection

type Record = dict[str, JsonValue]
MAX_RECORD_BYTES: Final = 256 * 1024
MAX_PHASE_BYTES: Final = 4 * 1024 * 1024
REQUIRED_TABLES: Final = 3
ACTION_RECEIPTS_VERSION: Final = 2
RECORD: Final = TypeAdapter(Record)


class RecoveryModel(BaseModel):
    """Inspect bounded legacy records without dropping their unrelated saved fields."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="ignore")


class RecoveryAttempt(RecoveryModel):
    """A failed reconciliation stays visible instead of being polled every two seconds."""

    status: Literal["running", "blocked", "completed"]
    driver_id: str
    count: int


class RecoveryIntent(RecoveryModel):
    """Original owner remains immutable; a recovery driver only borrows its lease."""

    id: str
    goal_id: str
    authority_epoch: int
    owner_id: str
    status: str
    lease_until: str
    pid: int | None
    birth: float | None
    recovery: RecoveryAttempt | None = None


class RecoveryClock(RecoveryModel):
    """Current human intent is the authority, not the saved graph checkpoint."""

    project: str
    goal_id: str
    authority_epoch: int
    active_intent_id: str
    enabled: bool
    user_intent: str
    closed: bool
    hold: Record


class RecoveryHold(RecoveryModel):
    """Only holds produced by this lifecycle may be cleared through receipt recovery."""

    kind: str
    guard_id: str
    source: str = ""


@dataclass(frozen=True, slots=True)
class RecoveryClaim:
    """A candidate is checked again under the original graph lock before adoption."""

    reference: HostRef
    driver_id: str
    record: Record


def _ready(clock: RecoveryClock, intent: RecoveryIntent) -> bool:
    if (
        not clock.enabled
        or clock.closed
        or clock.user_intent != "RUN"
        or clock.active_intent_id != intent.id
        or clock.goal_id != intent.goal_id
        or clock.authority_epoch != intent.authority_epoch
        or intent.status not in {"claimed", "started", "unknown"}
    ):
        return False
    if intent.recovery is not None and intent.recovery.status == "blocked":
        return False
    if clock.hold:
        hold = RecoveryHold.model_validate(clock.hold)
        return (
            hold.kind == "unknown"
            and hold.guard_id == intent.id
            and hold.source in {"round_result", "lease_expired", "service_shutdown"}
        )
    expires = datetime.fromisoformat(intent.lease_until)
    return expires.tzinfo is not None and expires <= datetime.now(UTC)


def _reference(clock: RecoveryClock, intent: RecoveryIntent) -> HostRef:
    return HostRef(
        project=clock.project,
        goal_id=clock.goal_id,
        operation_id=intent.id,
        epoch=intent.authority_epoch,
        owner_id=intent.owner_id,
    )


def _phase_ready(database: Connection, reference: HostRef) -> bool:
    identity = sha256(reference.model_dump_json().encode()).hexdigest()
    rows = text_cursor(
        database,
        """SELECT step,scope_hash,CAST(version AS TEXT),status,
        COALESCE(CASE WHEN length(CAST(result_json AS BLOB)) <= ? THEN result_json ELSE '' END, '')
        FROM goal_graph_steps WHERE intent_id=? LIMIT 4""",
        (MAX_PHASE_BYTES, reference.operation_id),
    ).fetchall()
    phases = {row[0]: row for row in rows}
    if any(row[1] != identity for row in rows):
        return False
    snapshot = phases.get("snapshot")
    native = phases.get("native")
    actions = phases.get("actions")
    settle = phases.get("settle")
    if (
        snapshot is None
        or native is None
        or set(phases) - {"snapshot", "native", "actions", "settle"}
        or snapshot[2:4] != ("1", "done")
        or native[2:4] != ("1", "done")
        or (actions is not None and (actions[2] != "2" or actions[3] not in {"inflight", "done"}))
    ):
        return False
    inputs = HostSnapshot.model_validate_json(snapshot[4])
    _ = HostNativeResult.model_validate_json(native[4])
    if settle is not None:
        if (
            inputs.settle_receipts_version != ACTION_RECEIPTS_VERSION
            or settle[2:4] != ("2", "inflight")
            or actions is None
            or actions[3] != "done"
        ):
            return False
        _ = HostApplied.model_validate_json(actions[4])
    return (
        inputs.action_receipts_version == ACTION_RECEIPTS_VERSION
        and inputs.plan_revision is not None
    )


def _load(database: Connection, goal_id: str) -> tuple[Record, Record] | None:
    ready = text_cursor(
        database,
        """SELECT name FROM sqlite_master WHERE type='table'
        AND name IN ('continuation_clocks','continuation_intents','goal_graph_steps')""",
    ).fetchall()
    if len(ready) != REQUIRED_TABLES:
        return None
    row = text_cursor(
        database,
        """SELECT data FROM continuation_clocks
        WHERE goal_id=? AND length(CAST(data AS BLOB)) <= ?""",
        (goal_id, MAX_RECORD_BYTES),
    ).fetchone()
    if row is None:
        return None
    clock = RECORD.validate_json(row[0])
    parsed = RecoveryClock.model_validate(clock)
    owner = text_cursor(
        database,
        """SELECT data FROM continuation_intents WHERE id=? AND goal_id=?
        AND length(CAST(data AS BLOB)) <= ?""",
        (parsed.active_intent_id, goal_id, MAX_RECORD_BYTES),
    ).fetchone()
    return None if owner is None else (clock, RECORD.validate_json(owner[0]))


def candidate(project: str, goal_id: str, driver_id: str) -> RecoveryClaim | None:
    """Invalid or incomplete original metadata keeps its existing UNKNOWN hold."""
    try:
        return _candidate(project, goal_id, driver_id)
    except ValueError:
        return None


def _candidate(project: str, goal_id: str, driver_id: str) -> RecoveryClaim | None:
    """Read original receipts only; browsing state must never acquire execution authority."""
    with continuation_store.readonly(project) as database:
        if database is None or (records := _load(database, goal_id)) is None:
            return None
        clock = RecoveryClock.model_validate(records[0])
        intent = RecoveryIntent.model_validate(records[1])
        if clock.project != project or clock.goal_id != goal_id or not _ready(clock, intent):
            return None
        reference = _reference(clock, intent)
        if not _phase_ready(database, reference) or intent.pid is None or intent.birth is None:
            return None
        verdict, _reason = goal_brain_runner.inspect_owned(intent.pid, intent.birth)
        if verdict != "exited":
            return None
        return RecoveryClaim(reference, driver_id, records[1])


def adopt(claim: RecoveryClaim) -> bool:
    """Inside the graph lock, borrow one lease without changing owner, epoch or cadence."""
    reference = claim.reference
    with continuation_store.mutation(reference.project, reference.goal_id) as database:
        records = _load(database, reference.goal_id)
        if records is None:
            return False
        clock = RecoveryClock.model_validate(records[0])
        intent = RecoveryIntent.model_validate(records[1])
        if (
            not _ready(clock, intent)
            or _reference(clock, intent) != reference
            or not _phase_ready(database, reference)
            or intent.pid is None
            or intent.birth is None
        ):
            return False
        verdict, _reason = goal_brain_runner.inspect_owned(intent.pid, intent.birth)
        if verdict != "exited":
            return False
        now = datetime.now(UTC)
        attempt = RecoveryAttempt(
            status="running",
            driver_id=claim.driver_id,
            count=1 if intent.recovery is None else intent.recovery.count + 1,
        )
        records[1].update(
            status="claimed",
            lease_until=(now + timedelta(seconds=120)).isoformat(),
            recovery=RECORD.validate_json(attempt.model_dump_json()),
        )
        records[0]["hold"] = {}
        continuation_store.write_intent(database, records[1])
        continuation_store.write(database, records[0])
    return True
