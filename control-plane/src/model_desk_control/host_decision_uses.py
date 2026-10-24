"""Original project SQL receipts bind one human approval to one stage's task."""

from sqlite3 import Connection
from typing import Final, Literal

from pydantic import Field, ValidationError

from model_desk_control.database import text_cursor
from model_desk_control.host_decision_scope import Authority, SavedRecord
from model_desk_control.host_flow import HostRecord
from model_desk_control.host_trace_models import RecordId
from model_desk_control.review_artifacts import ReviewScopeError

MAX_APPROVALS: Final = 200
MAX_BYTES: Final = 2 * 1024 * 1024
SCHEMA: Final = """CREATE TABLE IF NOT EXISTS human_decision_uses (
decision_id TEXT NOT NULL, stage_id TEXT NOT NULL, task_id TEXT NOT NULL,
data TEXT NOT NULL CHECK(json_valid(data)), PRIMARY KEY(decision_id,stage_id))"""


class Option(SavedRecord):
    """Continue is an explicit label, never the first option's position."""

    id: str
    label: str


class Proposal(SavedRecord):
    """Only the originally reviewed affected stages may consume the answer."""

    kind: Literal["exception", "final"]
    affected_stage_ids: tuple[RecordId, ...] = Field(max_length=200)
    options: tuple[Option, ...] = Field(max_length=12)
    approval_option: str = ""
    approval_option_id: str = ""


class Approval(HostRecord):
    """Keep raw strings so the original proposal/subject relationship can be checked."""

    id: RecordId
    goal_id: RecordId
    subject_hash: str
    payload: str
    fingerprint: str
    answer: str


class DecisionUse(HostRecord):
    """An immutable use is committed in the same transaction as the original stage claim."""

    version: Literal[1] = 1
    project: str
    goal_id: RecordId
    decision_id: RecordId
    stage_id: RecordId
    task_id: RecordId
    subject_hash: str
    authority: Authority
    execution_digest: str


def exists(database: Connection, table: str) -> bool:
    """Read-only capability discovery does not create governance tables."""
    return (
        text_cursor(
            database, "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def approvals(database: Connection, goal: str, stage: str) -> tuple[Approval, ...]:
    """Refuse incomplete history rather than selecting a convenient approval from a partial list."""
    if not exists(database, "human_decisions"):
        return ()
    ids = text_cursor(
        database,
        """SELECT id FROM human_decisions WHERE goal_id=? AND kind='exception'
        AND status='answered' ORDER BY id LIMIT ?""",
        (goal, MAX_APPROVALS + 1),
    ).fetchall()
    if len(ids) > MAX_APPROVALS:
        code = "review_approval_window"
        raise ReviewScopeError(code)
    remaining, result = MAX_BYTES, list[Approval]()
    try:
        for identity in ids:
            row = text_cursor(
                database,
                """SELECT id,goal_id,subject_hash,payload,fingerprint,answer FROM human_decisions
                WHERE id=? AND goal_id=? AND
                length(CAST(payload AS BLOB))+length(CAST(fingerprint AS BLOB))<=?""",
                (identity[0], goal, remaining),
            ).fetchone()
            if row is None:
                code = "review_approval_budget"
                raise ReviewScopeError(code)
            remaining -= len(row[3].encode()) + len(row[4].encode())
            proposal = Proposal.model_validate_json(row[3])
            approved = (
                bool(proposal.approval_option)
                and sum(
                    option.id == row[5] == proposal.approval_option_id
                    and option.label == proposal.approval_option
                    for option in proposal.options
                )
                == 1
            )
            if stage in proposal.affected_stage_ids and approved:
                if proposal.kind != "exception":
                    code = "review_approval_identity"
                    raise ReviewScopeError(code)
                result.append(
                    Approval(
                        id=row[0],
                        goal_id=row[1],
                        subject_hash=row[2],
                        payload=row[3],
                        fingerprint=row[4],
                        answer=row[5],
                    )
                )
    except ValidationError as error:
        code = "review_approval_unreadable"
        raise ReviewScopeError(code) from error
    return tuple(result)


def read_use(database: Connection, decision: str, stage: str) -> DecisionUse | None:
    """A missing table means unused, never permission to ignore a stored answer."""
    if not exists(database, "human_decision_uses"):
        return None
    row = text_cursor(
        database,
        """SELECT task_id,CASE WHEN length(CAST(data AS BLOB))<=65536 THEN data ELSE '' END
        FROM human_decision_uses WHERE decision_id=? AND stage_id=?""",
        (decision, stage),
    ).fetchone()
    if row is None:
        return None
    try:
        use = DecisionUse.model_validate_json(row[1])
    except ValidationError as error:
        code = "review_use_unreadable"
        raise ReviewScopeError(code) from error
    if use.decision_id != decision or use.stage_id != stage or use.task_id != row[0]:
        code = "review_use_identity"
        raise ReviewScopeError(code)
    return use


def save_use(database: Connection, use: DecisionUse) -> None:
    """The caller owns the transaction and the goal's existing writeback lock."""
    _ = database.execute(SCHEMA)
    _ = database.execute(
        "INSERT INTO human_decision_uses(decision_id,stage_id,task_id,data) VALUES(?,?,?,?)",
        (use.decision_id, use.stage_id, use.task_id, use.model_dump_json()),
    )


def assigned(database: Connection, use: DecisionUse, *, allow_reclaim: bool = False) -> bool:
    """A receipt cannot reactivate its task after a stage was cancelled or reassigned."""
    row = text_cursor(
        database,
        "SELECT active_task_id,status,goal_id,project FROM stages WHERE id=?",
        (use.stage_id,),
    ).fetchone()
    if row is None or row[2] != use.goal_id or row[3] != use.project:
        return False
    active = row[0] == use.task_id and row[1] in {"dispatched", "running"}
    reclaim = (
        allow_reclaim
        and not row[0]
        and row[1]
        in {
            "pending",
            "ready",
            "returned",
            "executor_completed",
        }
    )
    return active or reclaim
