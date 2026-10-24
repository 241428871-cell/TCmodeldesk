"""A durable queue in the original project store, with no scheduler of its own."""

from datetime import UTC, datetime
from hashlib import sha256
from sqlite3 import Connection
from typing import Final

from model_desk_control.database import text_cursor
from model_desk_control.host_events import exists
from model_desk_control.host_flow import HostRef
from model_desk_control.host_queue_models import QueueError, QueueState, Ticket

MAX_RECEIPT: Final = 16 * 1024


def read(database: Connection, task_id: str) -> QueueState | None:
    """Read only; importing or inspecting a queue never initializes records."""
    if not exists(database, "goal_task_queue"):
        return None
    row = text_cursor(
        database,
        "SELECT goal_id,phase,substr(data,1,16385) FROM goal_task_queue WHERE task_id=?",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    if len(row[2].encode()) > MAX_RECEIPT:
        code = "queue_receipt_budget"
        raise QueueError(code)
    result = QueueState.model_validate_json(row[2])
    if (result.ticket.task_id, result.ticket.goal_id, result.phase.value) != (
        task_id,
        row[0],
        row[1],
    ):
        code = "queue_receipt_identity"
        raise QueueError(code)
    return result


def origin(database: Connection, ticket: Ticket) -> None:
    """The original stage claim and original brain request must agree with the saved task."""
    stage = text_cursor(
        database, "SELECT project,goal_id,active_task_id FROM stages WHERE id=?", (ticket.stage_id,)
    ).fetchone()
    if stage is None or tuple(stage) != (ticket.project, ticket.goal_id, ticket.task_id):
        code = "queue_stage_identity"
        raise QueueError(code)
    row = text_cursor(
        database,
        """SELECT goal_id,CAST(json_extract(data,'$.authority_epoch') AS TEXT),
        json_extract(data,'$.owner_id') FROM continuation_intents WHERE id=?""",
        (ticket.round_id,),
    ).fetchone()
    if row is None or row[0] != ticket.goal_id or row[1] != str(ticket.epoch):
        code = "queue_round_identity"
        raise QueueError(code)
    reference = HostRef(
        project=ticket.project,
        goal_id=ticket.goal_id,
        operation_id=ticket.round_id,
        epoch=ticket.epoch,
        owner_id=row[2],
    )
    owner = "goal_" + sha256(reference.model_dump_json().encode()).hexdigest()
    request = text_cursor(
        database,
        "SELECT kind,payload_digest FROM brain_requests WHERE owner=? AND request_id=?",
        (owner, ticket.request_id),
    ).fetchone()
    if request is None or tuple(request) != ("dispatch", ticket.payload_digest):
        code = "queue_request_identity"
        raise QueueError(code)


def write(database: Connection, state: QueueState, previous: QueueState | None) -> None:
    """A transition and its audit receipt commit inside the caller's original transaction."""
    if not database.in_transaction:
        code = "queue_transaction_required"
        raise QueueError(code)
    if previous is None:
        _ = database.execute("""CREATE TABLE IF NOT EXISTS goal_task_queue (
            task_id TEXT PRIMARY KEY, goal_id TEXT NOT NULL, phase TEXT NOT NULL,
            data TEXT NOT NULL CHECK(json_valid(data)))""")
        _ = database.execute("""CREATE INDEX IF NOT EXISTS goal_task_queue_goal
            ON goal_task_queue(goal_id,phase)""")
        _ = database.execute(
            "INSERT INTO goal_task_queue VALUES(?,?,?,?)",
            (
                state.ticket.task_id,
                state.ticket.goal_id,
                state.phase.value,
                state.model_dump_json(),
            ),
        )
    else:
        changed = database.execute(
            "UPDATE goal_task_queue SET phase=?,data=? WHERE task_id=? AND data=?",
            (
                state.phase.value,
                state.model_dump_json(),
                state.ticket.task_id,
                previous.model_dump_json(),
            ),
        )
        if changed.rowcount != 1:
            code = "queue_transition_conflict"
            raise QueueError(code)
    _ = database.execute(
        """INSERT INTO stage_log(at,goal_id,stage_id,task_id,action,actor,detail)
        VALUES(?,?,?,?,?,?,?)""",
        (
            datetime.now(UTC).isoformat(),
            state.ticket.goal_id,
            state.ticket.stage_id,
            state.ticket.task_id,
            "queue_transition",
            "queue-driver",
            state.model_dump_json(),
        ),
    )


def prepare(database: Connection, ticket: Ticket) -> QueueState:
    """Persist readiness only after all original task inputs exist and match."""
    origin(database, ticket)
    previous = read(database, ticket.task_id)
    if previous is not None:
        if previous.ticket != ticket:
            code = "queue_input_changed"
            raise QueueError(code)
        return previous
    state = QueueState(ticket=ticket)
    write(database, state, None)
    return state


def candidates(database: Connection, goal_id: str) -> tuple[str, ...]:
    """The existing driver inspects at most 64 prelaunch entries, not the model or log files."""
    if not exists(database, "goal_task_queue"):
        return ()
    return tuple(
        row[0]
        for row in text_cursor(
            database,
            """SELECT task_id FROM goal_task_queue WHERE goal_id=?
        AND phase IN ('ready','queued','claimed') ORDER BY rowid LIMIT 64""",
            (goal_id,),
        ).fetchall()
    )
