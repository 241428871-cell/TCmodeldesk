"""Typed relational reads and immutable observation receipts for planning rounds."""

from sqlite3 import Connection

from model_desk_control.database import StoreError, text_cursor
from model_desk_control.round_models import BrainFeedback, PlanningRound


def read_round(database: Connection, goal_id: str, operation_id: str) -> PlanningRound | None:
    """Return only an exact goal-bound operation; no record is fabricated on read."""
    row = text_cursor(
        database,
        "SELECT document FROM planning_rounds WHERE goal_id = ? AND operation_id = ?",
        (goal_id, operation_id),
    ).fetchone()
    if row is None:
        return None
    record = PlanningRound.model_validate_json(row[0])
    if record.request.operation_id != operation_id or record.request.control.goal_id != goal_id:
        code = "round_record_identity_mismatch"
        raise StoreError(code)
    return record


def require_round(database: Connection, goal_id: str, operation_id: str) -> PlanningRound:
    """Missing domain state cannot be recovered by trusting a stale graph payload."""
    record = read_round(database, goal_id, operation_id)
    if record is None:
        code = "planning_round_not_found"
        raise StoreError(code)
    return record


def write_round(database: Connection, record: PlanningRound) -> None:
    """Update an exact operation inside the caller's exclusive transaction."""
    cursor = database.execute(
        "UPDATE planning_rounds SET document = ? WHERE goal_id = ? AND operation_id = ?",
        (record.model_dump_json(), record.request.control.goal_id, record.request.operation_id),
    )
    if cursor.rowcount != 1:
        code = "planning_round_not_found"
        raise StoreError(code)


def record_feedback(database: Connection, feedback: BrainFeedback) -> bool:
    """Reject conflicting receipt or sequence reuse; duplicate observations are harmless."""
    rows = text_cursor(
        database,
        """SELECT document FROM round_feedback WHERE operation_id = ?
           AND (receipt_id = ? OR sequence = ?)""",
        (feedback.operation_id, feedback.receipt_id, str(feedback.sequence)),
    ).fetchall()
    if rows:
        if len(rows) != 1 or BrainFeedback.model_validate_json(rows[0][0]) != feedback:
            code = "runner_receipt_conflict"
            raise StoreError(code)
        return False
    _ = database.execute(
        "INSERT INTO round_feedback VALUES (?, ?, ?, ?)",
        (feedback.operation_id, feedback.receipt_id, feedback.sequence, feedback.model_dump_json()),
    )
    return True
