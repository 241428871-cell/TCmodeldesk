"""Branch facts in the original project store; checkpoints only record graph position."""

from datetime import UTC, datetime
from enum import StrEnum
from sqlite3 import Connection
from typing import Annotated, Final

from pydantic import Field

from model_desk_control.database import text_cursor
from model_desk_control.host_branch import BranchRef
from model_desk_control.host_events import exists
from model_desk_control.host_flow import HostRecord

TABLE: Final = "goal_task_branches"
MAX_LIVE: Final = 64
# Separate statements: executescript would commit the caller's open transaction.
SCHEMA: Final = (
    """CREATE TABLE IF NOT EXISTS goal_task_branches (
 branch_id TEXT PRIMARY KEY, project TEXT NOT NULL, goal_id TEXT NOT NULL,
 stage_id TEXT NOT NULL, epoch INTEGER NOT NULL, origin_intent_id TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('open','waiting','dispatched','retired')),
 decision_id TEXT NOT NULL DEFAULT '', task_id TEXT NOT NULL DEFAULT '',
 reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS goal_task_branch_live
 ON goal_task_branches(goal_id, stage_id) WHERE status IN ('open','waiting')""",
)


class BranchError(ValueError):
    """A saved branch conflicts with its identity or an already settled outcome."""

    def __init__(self, code: str) -> None:
        """Retain a stable machine reason without stage text or native output."""
        self.code: str = code
        super().__init__(code)


class BranchStatus(StrEnum):
    """Live branches may wait or proceed; settled branches never reopen."""

    OPEN = "open"
    WAITING = "waiting"
    DISPATCHED = "dispatched"
    RETIRED = "retired"


LIVE: Final = frozenset({BranchStatus.OPEN, BranchStatus.WAITING})


class BranchMark(HostRecord):
    """One requested transition; waiting names its card, dispatch names its task."""

    status: BranchStatus
    decision_id: Annotated[str, Field(max_length=100)] = ""
    task_id: Annotated[str, Field(max_length=300)] = ""
    reason: Annotated[str, Field(max_length=1000)] = ""


class BranchRecord(BranchMark):
    """A compact domain row: references and status, never prompts or answers."""

    reference: BranchRef


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _record(row: tuple[str, ...]) -> BranchRecord:
    return BranchRecord(
        reference=BranchRef(
            branch_id=row[0],
            project=row[1],
            goal_id=row[2],
            stage_id=row[3],
            epoch=int(row[4]),
            origin_operation_id=row[5],
        ),
        status=BranchStatus(row[6]),
        decision_id=row[7],
        task_id=row[8],
        reason=row[9],
    )


def read(database: Connection, branch_id: str) -> BranchRecord | None:
    """Read only; an absent table means no branch was ever opened in this project."""
    if not exists(database, TABLE):
        return None
    row = text_cursor(
        database,
        """SELECT branch_id,project,goal_id,stage_id,CAST(epoch AS TEXT),origin_intent_id,
        status,decision_id,task_id,reason FROM goal_task_branches WHERE branch_id=?""",
        (branch_id,),
    ).fetchone()
    return None if row is None else _record(tuple(row))


def live(database: Connection, goal_id: str) -> tuple[BranchRecord, ...]:
    """A bounded inbox of unsettled branches for the owning goal's current round."""
    if not exists(database, TABLE):
        return ()
    rows = text_cursor(
        database,
        """SELECT branch_id,project,goal_id,stage_id,CAST(epoch AS TEXT),origin_intent_id,
        status,decision_id,task_id,reason FROM goal_task_branches
        WHERE goal_id=? AND status IN ('open','waiting') ORDER BY created_at, branch_id LIMIT ?""",
        (goal_id, MAX_LIVE),
    ).fetchall()
    return tuple(_record(tuple(row)) for row in rows)


def _require_transaction(database: Connection) -> None:
    if not database.in_transaction:
        code = "branch_transaction_required"
        raise BranchError(code)


def _live_for_stage(database: Connection, goal_id: str, stage_id: str) -> BranchRecord | None:
    row = text_cursor(
        database,
        """SELECT branch_id FROM goal_task_branches
        WHERE goal_id=? AND stage_id=? AND status IN ('open','waiting')""",
        (goal_id, stage_id),
    ).fetchone()
    return None if row is None else read(database, row[0])


def open_branch(database: Connection, reference: BranchRef) -> BranchRecord:
    """Reuse this stage's live branch in the same epoch; older epochs are retired, not revived."""
    _require_transaction(database)
    for statement in SCHEMA:
        _ = database.execute(statement)
    existing = read(database, reference.branch_id)
    if existing is not None:
        if existing.reference != reference:
            code = "branch_identity_conflict"
            raise BranchError(code)
        return existing
    current = _live_for_stage(database, reference.goal_id, reference.stage_id)
    if current is not None:
        if (current.reference.project, current.reference.epoch) == (
            reference.project,
            reference.epoch,
        ):
            return current
        retired = BranchMark(status=BranchStatus.RETIRED, reason="epoch_changed")
        _ = mark(database, current.reference.branch_id, retired)
    now = _now()
    _ = database.execute(
        """INSERT INTO goal_task_branches(branch_id,project,goal_id,stage_id,epoch,
        origin_intent_id,status,created_at,updated_at) VALUES (?,?,?,?,?,?,'open',?,?)""",
        (
            reference.branch_id,
            reference.project,
            reference.goal_id,
            reference.stage_id,
            reference.epoch,
            reference.origin_operation_id,
            now,
            now,
        ),
    )
    created = read(database, reference.branch_id)
    if created is None:
        code = "branch_write_lost"
        raise BranchError(code)
    return created


def mark(database: Connection, branch_id: str, target: BranchMark) -> BranchRecord:
    """Settled outcomes are immutable; repeating the same outcome is an idempotent replay."""
    _require_transaction(database)
    current = read(database, branch_id)
    if current is None:
        code = "branch_missing"
        raise BranchError(code)
    if current.status not in LIVE:
        if (current.status, current.task_id) == (target.status, target.task_id):
            return current
        code = "branch_already_settled"
        raise BranchError(code)
    if target.status is BranchStatus.WAITING and not target.decision_id:
        code = "branch_wait_requires_decision"
        raise BranchError(code)
    _ = database.execute(
        """UPDATE goal_task_branches SET status=?,decision_id=?,task_id=?,reason=?,updated_at=?
        WHERE branch_id=?""",
        (
            target.status.value,
            target.decision_id,
            target.task_id,
            target.reason,
            _now(),
            branch_id,
        ),
    )
    return BranchRecord(
        reference=current.reference,
        status=target.status,
        decision_id=target.decision_id,
        task_id=target.task_id,
        reason=target.reason,
    )
