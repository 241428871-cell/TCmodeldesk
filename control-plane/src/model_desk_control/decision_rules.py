"""Database-owned scope checks and invalidation shared by all host adapters."""

from collections.abc import Sequence
from sqlite3 import Connection

from model_desk_control.authority import read_control
from model_desk_control.control import UserIntent
from model_desk_control.database import StoreError, text_cursor
from model_desk_control.decisions import Decision, DecisionScope, DecisionStatus
from model_desk_control.task_state import TaskStatus, read_plan, read_task, task_digest


def _parse_row(row: Sequence[str], goal_id: str) -> Decision:
    decision = Decision.model_validate_json(row[4])
    if (
        decision.scope.goal_id != goal_id
        or decision.decision_id != row[0]
        or decision.subject_hash != row[1]
        or decision.revision != int(row[2])
        or decision.status != row[3]
    ):
        code = "decision_record_identity_mismatch"
        raise StoreError(code)
    return decision


def read_decision(database: Connection, goal_id: str, decision_id: str) -> Decision:
    """Goal scope is mandatory, even when the global card identity is known."""
    row = text_cursor(
        database,
        """SELECT decision_id, subject_hash, CAST(revision AS TEXT), state, document
           FROM decisions WHERE goal_id = ? AND decision_id = ?""",
        (goal_id, decision_id),
    ).fetchone()
    if row is None:
        code = "decision_not_found"
        raise StoreError(code)
    return _parse_row(row, goal_id)


def write_decision(database: Connection, decision: Decision) -> None:
    """Write inside an exclusive transaction without changing its subject identity."""
    cursor = database.execute(
        """UPDATE decisions SET revision = ?, state = ?, document = ?
           WHERE goal_id = ? AND decision_id = ?""",
        (
            decision.revision,
            decision.status,
            decision.model_dump_json(),
            decision.scope.goal_id,
            decision.decision_id,
        ),
    )
    if cursor.rowcount != 1:
        code = "decision_not_found"
        raise StoreError(code)


def invalid_reason(database: Connection, scope: DecisionScope) -> str | None:
    """Validate relevant facts against the live domain, never a graph checkpoint."""
    control = read_control(database, scope.goal_id)
    if control.closed or control.intent is not UserIntent.RUN or control.epoch != scope.epoch:
        return "authority_changed"
    plan = read_plan(database, scope.goal_id)
    task = next((task for task in plan.tasks if task.task_id == scope.task_id), None)
    if task is None or scope.task_id in plan.superseded:
        return "task_superseded"
    if task_digest(task) != scope.task_hash:
        return "task_content_changed"
    state = read_task(database, scope.goal_id, scope.task_id)
    if state.status in (TaskStatus.COMPLETED, TaskStatus.CANCELLED):
        return "task_finished"
    checks = (
        (state.policy_revision != scope.policy_revision, "policy_changed"),
        (state.artifact_hash != scope.artifact_hash, "artifact_changed"),
        (state.context_revision != scope.context_revision, "task_context_changed"),
    )
    return next((reason for changed, reason in checks if changed), None)


def open_decisions(database: Connection, goal_id: str) -> tuple[Decision, ...]:
    """Return recorded pending and answered cards, including stale ones for reconciliation."""
    rows = text_cursor(
        database,
        """SELECT decision_id, subject_hash, CAST(revision AS TEXT), state, document
           FROM decisions WHERE goal_id = ?
           AND state IN ('pending', 'answered') ORDER BY decision_id""",
        (goal_id,),
    ).fetchall()
    return tuple(_parse_row(row, goal_id) for row in rows)


def invalidate_stale(database: Connection, goal_id: str) -> int:
    """Mark stale cards explicitly; this deterministic sweep starts no graph or CLI."""
    count = 0
    for decision in open_decisions(database, goal_id):
        reason = invalid_reason(database, decision.scope)
        if reason is not None:
            write_decision(
                database,
                decision.model_copy(
                    update={
                        "status": DecisionStatus.INVALIDATED,
                        "revision": decision.revision + 1,
                        "invalid_reason": reason,
                    }
                ),
            )
            count += 1
    return count


def revoke_goal_decisions(database: Connection, goal_id: str) -> int:
    """STOP revocation must not depend on parsing potentially damaged task or card data."""
    cursor = database.execute(
        """UPDATE decisions SET state = 'invalidated', revision = revision + 1,
           document = json_set(document, '$.status', 'invalidated',
               '$.revision', revision + 1, '$.invalid_reason', 'authority_changed')
           WHERE goal_id = ? AND state IN ('pending', 'answered')""",
        (goal_id,),
    )
    return cursor.rowcount
