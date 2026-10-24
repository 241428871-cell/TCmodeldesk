"""Read live goal authority inside an existing domain transaction."""

from sqlite3 import Connection

from model_desk_control.control import Control
from model_desk_control.database import StoreError, text_cursor


def read_control(database: Connection, goal_id: str) -> Control:
    """Missing authority is never reconstructed from receipts or checkpoints."""
    row = text_cursor(
        database,
        """SELECT CAST(epoch AS TEXT), CAST(revision AS TEXT), document
           FROM controls WHERE goal_id = ?""",
        (goal_id,),
    ).fetchone()
    if row is None:
        code = "goal_not_found"
        raise StoreError(code)
    control = Control.model_validate_json(row[2])
    if (
        control.goal_id != goal_id
        or control.epoch != int(row[0])
        or control.revision != int(row[1])
    ):
        code = "control_record_identity_mismatch"
        raise StoreError(code)
    return control


def write_control(database: Connection, prior: Control, changed: Control) -> None:
    """Replace exact current authority inside the caller's exclusive transaction."""
    if changed.goal_id != prior.goal_id:
        code = "control_record_identity_mismatch"
        raise StoreError(code)
    cursor = database.execute(
        """UPDATE controls SET epoch = ?, revision = ?, document = ?
           WHERE goal_id = ? AND epoch = ? AND revision = ?""",
        (
            changed.epoch,
            changed.revision,
            changed.model_dump_json(),
            prior.goal_id,
            prior.epoch,
            prior.revision,
        ),
    )
    if cursor.rowcount != 1:
        code = "concurrent_control_change"
        raise StoreError(code)
