"""Initialization admission shares the original stage-claim transaction."""

from datetime import UTC, datetime
from sqlite3 import Connection

from model_desk_control.database import text_cursor
from model_desk_control.host_events import exists
from model_desk_control.host_initialization_models import (
    MAX_INITIALIZATION,
    InitialDraft,
    InitializationError,
    InitialKey,
    InitialPhase,
    InitialRecord,
)
from model_desk_control.host_queue_models import Ticket
from model_desk_control.host_queue_store import origin


def read(database: Connection, task_id: str) -> InitialRecord | None:
    """Read without creating tables, dispatching, or filling any missing task files."""
    if not exists(database, "goal_task_initializations"):
        return None
    row = text_cursor(
        database,
        """SELECT goal_id,round_id,request_id,status,substr(data,1,?)
        FROM goal_task_initializations WHERE task_id=?""",
        (MAX_INITIALIZATION + 1, task_id),
    ).fetchone()
    if row is None:
        return None
    if len(row[4].encode()) > MAX_INITIALIZATION:
        code = "initialization_record_budget"
        raise InitializationError(code)
    result = InitialRecord.model_validate_json(row[4])
    saved = result.draft.ticket
    if (saved.task_id, saved.goal_id, saved.round_id, saved.request_id, result.phase.value) != (
        task_id,
        row[0],
        row[1],
        row[2],
        row[3],
    ):
        code = "initialization_record_identity"
        raise InitializationError(code)
    result.draft.verify()
    return result


def find(database: Connection, key: InitialKey) -> InitialRecord | None:
    """Resolve by the original request, never by newest task, model, or project."""
    if not exists(database, "goal_task_initializations"):
        return None
    row = text_cursor(
        database,
        """SELECT task_id FROM goal_task_initializations
        WHERE goal_id=? AND round_id=? AND request_id=?""",
        (key.goal_id, key.round_id, key.request_id),
    ).fetchone()
    result = None if row is None else read(database, row[0])
    if result is not None and not result.draft.matches(key):
        code = "initialization_request_identity"
        raise InitializationError(code)
    return result


def audit(database: Connection, record: InitialRecord) -> None:
    """Retain identity and input digest without duplicating prompt content into stage logs."""
    reference = record.draft.ticket
    _ = database.execute(
        """INSERT INTO stage_log(at,goal_id,stage_id,task_id,action,actor,detail)
        VALUES(?,?,?,?,?,?,?)""",
        (
            datetime.now(UTC).isoformat(),
            reference.goal_id,
            reference.stage_id,
            reference.task_id,
            "initialization_" + record.phase.value,
            "task-initializer",
            reference.model_dump_json(),
        ),
    )


def save(database: Connection, draft: InitialDraft) -> None:
    """Commit the exact input manifest with the original claim and approval consumption."""
    if not database.in_transaction:
        code = "initialization_transaction_required"
        raise InitializationError(code)
    draft.verify()
    origin(database, draft.ticket)
    old = read(database, draft.ticket.task_id)
    if old is not None:
        if old.draft != draft:
            code = "initialization_admission_conflict"
            raise InitializationError(code)
        return
    _ = database.execute("""CREATE TABLE IF NOT EXISTS goal_task_initializations (
        task_id TEXT PRIMARY KEY, goal_id TEXT NOT NULL, round_id TEXT NOT NULL,
        request_id TEXT NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL CHECK(json_valid(data)),
        UNIQUE(goal_id,round_id,request_id))""")
    record = InitialRecord(draft=draft)
    if len(record.model_dump_json().encode()) > MAX_INITIALIZATION:
        code = "initialization_record_budget"
        raise InitializationError(code)
    _ = database.execute(
        "INSERT INTO goal_task_initializations VALUES(?,?,?,?,?,?)",
        (
            draft.ticket.task_id,
            draft.ticket.goal_id,
            draft.ticket.round_id,
            draft.ticket.request_id,
            record.phase.value,
            record.model_dump_json(),
        ),
    )
    audit(database, record)


def complete(database: Connection, ticket: Ticket) -> None:
    """Queue readiness and completed initialization commit in the same original transaction."""
    record = read(database, ticket.task_id)
    if record is None:
        return
    if not database.in_transaction or record.draft.ticket != ticket:
        code = "initialization_completion_identity"
        raise InitializationError(code)
    if record.phase == InitialPhase.READY:
        return
    finished = InitialRecord(draft=record.draft, phase=InitialPhase.READY)
    updated = database.execute(
        "UPDATE goal_task_initializations SET status=?,data=? WHERE task_id=? AND data=?",
        (
            finished.phase.value,
            finished.model_dump_json(),
            ticket.task_id,
            record.model_dump_json(),
        ),
    )
    if updated.rowcount != 1:
        code = "initialization_completion_conflict"
        raise InitializationError(code)
    audit(database, finished)
