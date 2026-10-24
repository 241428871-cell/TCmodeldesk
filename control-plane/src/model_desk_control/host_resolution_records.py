"""Bounded original-round facts from the existing project database."""

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from sqlite3 import Connection
from typing import Final

from pydantic import TypeAdapter

from model_desk_control.database import text_cursor
from model_desk_control.host_flow import HostRef
from model_desk_control.host_resolution_models import (
    PhaseFact,
    Record,
    RequestFact,
    ResolutionClock,
    ResolutionError,
    ResolutionIntent,
    ResolutionRecords,
)

MAX_RECORD: Final = 256 * 1024
MAX_PHASE: Final = 4 * 1024 * 1024
MAX_PHASES: Final = 4
MAX_REQUESTS: Final = 500
MAX_RECEIPT: Final = 2 * 1024 * 1024
MAX_RECEIPTS_TOTAL: Final = 4 * 1024 * 1024
RECORD: Final = TypeAdapter(Record)


@dataclass(frozen=True, slots=True)
class JournalFacts:
    """Missing windows block disposition rather than omitting unexamined effects."""

    phases: tuple[PhaseFact, ...]
    requests: tuple[RequestFact, ...]
    blockers: tuple[str, ...]


def digest(value: str) -> str:
    """Bind decisions to actual stored bytes without exposing their contents."""
    return sha256(value.encode()).hexdigest()


def has_table(database: Connection, name: str) -> bool:
    """Schema inspection is read only and does not initialize an untouched project."""
    return (
        text_cursor(
            database, "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def load(
    database: Connection, project: Path, goal_id: str, intent_id: str | None = None
) -> ResolutionRecords:
    """A current browser project and SQL owner must agree with both JSON identities."""
    goal = text_cursor(
        database, "SELECT project,status FROM goals WHERE id=?", (goal_id,)
    ).fetchone()
    if goal is None or Path(goal[0]).resolve() != project:
        code = "resolution_project_mismatch"
        raise ResolutionError(code)
    if not has_table(database, "continuation_clocks"):
        code = "resolution_no_original_round"
        raise ResolutionError(code)
    row = text_cursor(
        database,
        """SELECT data FROM continuation_clocks WHERE goal_id=?
        AND length(CAST(data AS BLOB))<=?""",
        (goal_id, MAX_RECORD),
    ).fetchone()
    if row is None:
        code = "resolution_clock_unreadable"
        raise ResolutionError(code)
    raw_clock = RECORD.validate_json(row[0])
    clock = ResolutionClock.model_validate(raw_clock)
    selected = clock.active_intent_id if intent_id is None else intent_id
    original = text_cursor(
        database,
        """SELECT data,status FROM continuation_intents
        WHERE id=? AND goal_id=? AND length(CAST(data AS BLOB))<=?""",
        (selected, goal_id, MAX_RECORD),
    ).fetchone()
    if original is None:
        code = "resolution_no_original_round"
        raise ResolutionError(code)
    raw_intent = RECORD.validate_json(original[0])
    intent = ResolutionIntent.model_validate(raw_intent)
    if (
        clock.project != str(project)
        or clock.goal_id != goal_id
        or intent.id != selected
        or intent.goal_id != goal_id
        or intent.status != original[1]
    ):
        code = "resolution_original_identity_conflict"
        raise ResolutionError(code)
    return ResolutionRecords(raw_clock, raw_intent, clock, intent, goal[1])


def reference(records: ResolutionRecords, project: Path) -> HostRef:
    """The original owner remains visible after the human retires its lease."""
    intent = records.parsed_intent
    return HostRef(
        project=str(project),
        goal_id=intent.goal_id,
        operation_id=intent.id,
        epoch=intent.authority_epoch,
        owner_id=intent.owner_id,
    )


def _phases(database: Connection, scope: HostRef) -> tuple[tuple[PhaseFact, ...], tuple[str, ...]]:
    """Only phase hashes leave this bounded original-journal reader."""
    phases: list[PhaseFact] = []
    blockers: list[str] = []
    identity = digest(scope.model_dump_json())
    if has_table(database, "goal_graph_steps"):
        rows = text_cursor(
            database,
            """SELECT step,status,CAST(version AS TEXT),scope_hash,
            CAST(length(CAST(COALESCE(result_json,'') AS BLOB)) AS TEXT),
            CASE WHEN length(CAST(COALESCE(result_json,'') AS BLOB))<=?
            THEN COALESCE(result_json,'') ELSE '' END
            FROM goal_graph_steps WHERE intent_id=? ORDER BY step LIMIT ?""",
            (MAX_PHASE, scope.operation_id, MAX_PHASES + 1),
        ).fetchall()
        if len(rows) > MAX_PHASES:
            blockers.append("原轮次阶段超出当前核对范围")
        for row in rows[:MAX_PHASES]:
            if row[3] != identity:
                blockers.append("原轮次存在归属不符的阶段回执")
            if int(row[4]) > MAX_PHASE:
                blockers.append("原轮次阶段正文超过核对读取上限")
            phases.append(
                PhaseFact(
                    step=row[0],
                    status=row[1],
                    version=row[2],
                    scope_hash=row[3],
                    result_digest=digest(row[5]),
                )
            )
    return tuple(phases), tuple(dict.fromkeys(blockers))


def _requests(
    database: Connection, scope: HostRef
) -> tuple[tuple[RequestFact, ...], tuple[str, ...]]:
    """Bound actual receipt reads by the aggregate budget before opening each value."""
    requests: list[RequestFact] = []
    blockers: list[str] = []
    if has_table(database, "brain_requests"):
        owner = "goal_" + digest(scope.model_dump_json())
        rows = text_cursor(
            database,
            """SELECT request_id,kind,state,task_id,payload_digest,
            CAST(length(CAST(receipt AS BLOB)) AS TEXT)
            FROM brain_requests WHERE owner=? ORDER BY request_id LIMIT ?""",
            (owner, MAX_REQUESTS + 1),
        ).fetchall()
        if len(rows) > MAX_REQUESTS:
            blockers.append("原轮次动作超过核对窗口")
        remaining = MAX_RECEIPTS_TOTAL
        for row in rows[:MAX_REQUESTS]:
            receipt = ""
            length = int(row[5])
            if length > min(remaining, MAX_RECEIPT):
                blockers.append("原动作回执超过核对读取预算")
            else:
                saved = text_cursor(
                    database,
                    """SELECT receipt FROM brain_requests WHERE owner=?
                    AND request_id=? AND length(CAST(receipt AS BLOB))<=?""",
                    (owner, row[0], min(remaining, MAX_RECEIPT)),
                ).fetchone()
                if saved is None:
                    blockers.append("原动作回执在核对期间发生变化")
                else:
                    receipt = saved[0]
                    remaining -= len(receipt.encode())
            requests.append(
                RequestFact(
                    request_id=row[0],
                    kind=row[1],
                    state=row[2],
                    task_id=row[3],
                    payload_digest=row[4],
                    receipt_digest=digest(receipt),
                )
            )
    return tuple(requests), tuple(dict.fromkeys(blockers))


def journal(database: Connection, scope: HostRef) -> JournalFacts:
    """Read bounded phase and action receipts without changing their original status."""
    phases, phase_blockers = _phases(database, scope)
    requests, request_blockers = _requests(database, scope)
    return JournalFacts(
        phases, requests, tuple(dict.fromkeys([*phase_blockers, *request_blockers]))
    )
