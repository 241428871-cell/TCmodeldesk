"""Plan commit, continuation settlement and busy release form one domain transaction."""

from sqlite3 import Connection
from typing import assert_never

from model_desk_control.authority import read_control, write_control
from model_desk_control.control import ContractError, Control, UserIntent
from model_desk_control.database import StoreError, text_cursor
from model_desk_control.lease_rules import ContinuationLease
from model_desk_control.plan_commands import PatchCommand, commit_plan_patch
from model_desk_control.receipts import fingerprint
from model_desk_control.round_models import (
    FeedbackKind,
    PlanningRound,
    RoundDisposition,
    RoundStage,
)
from model_desk_control.round_records import require_round, write_round


def _apply_result(
    database: Connection, record: PlanningRound, control: Control
) -> tuple[RoundDisposition, int | None, str | None]:
    feedback = record.feedback
    if feedback is None or feedback.patch is None:
        code = "inconsistent_brain_feedback"
        raise StoreError(code)
    patch = feedback.patch
    if patch.base_revision != record.request.plan.revision:
        return RoundDisposition.REPLAN_REQUIRED, None, "brain_base_revision_mismatch"
    operation_hash = fingerprint(control.goal_id, record.request.operation_id)
    command = PatchCommand(
        goal_id=control.goal_id,
        request_id=f"round-plan:{operation_hash}",
        epoch=control.epoch,
        control_revision=control.revision,
        patch=patch,
    )
    try:
        plan = commit_plan_patch(database, command)
    except ContractError as error:
        return RoundDisposition.REPLAN_REQUIRED, None, error.code.value
    return RoundDisposition.APPLIED, plan.revision, None


def _disposition(
    database: Connection, record: PlanningRound
) -> tuple[RoundDisposition, int | None, str | None]:
    control = read_control(database, record.request.control.goal_id)
    if (
        control.closed
        or control.intent is not UserIntent.RUN
        or control.epoch != record.request.control.epoch
    ):
        return RoundDisposition.REVOKED, None, "authority_changed"
    feedback = record.feedback
    if feedback is None:
        return RoundDisposition.REVOKED, None, "round_never_armed"
    match feedback.kind:
        case FeedbackKind.RESULT:
            return _apply_result(database, record, control)
        case FeedbackKind.FAILED:
            return RoundDisposition.FAILED, None, feedback.reason
        case FeedbackKind.NOT_STARTED:
            return RoundDisposition.NOT_STARTED, None, feedback.reason
        case FeedbackKind.RUNNING | FeedbackKind.UNKNOWN:
            code = "planning_round_not_terminal"
            raise StoreError(code)
        case _:
            assert_never(feedback.kind)


def _settle(database: Connection, record: PlanningRound) -> None:
    goal_id = record.request.control.goal_id
    cursor = database.execute(
        "UPDATE outbox SET state = 'completed' WHERE goal_id = ? AND operation_id = ?",
        (goal_id, record.request.operation_id),
    )
    if cursor.rowcount != 1:
        code = "continuation_intent_not_found"
        raise StoreError(code)
    row = text_cursor(
        database,
        """SELECT CAST(count(*) AS TEXT) FROM outbox
           WHERE goal_id = ? AND state IN ('pending', 'leased', 'unknown')""",
        (goal_id,),
    ).fetchone()
    if row is None:
        code = "count_result_missing"
        raise StoreError(code)
    control = read_control(database, goal_id)
    busy = int(row[0]) > 0
    if busy != control.busy:
        write_control(
            database,
            control,
            control.model_copy(update={"busy": busy, "revision": control.revision + 1}),
        )


def finalize_round(database: Connection, lease: ContinuationLease) -> PlanningRound:
    """Settle only a terminal receipt or a domain-proven never-armed operation."""
    record = require_round(database, lease.goal_id, lease.operation_id)
    if record.request.control.epoch != lease.epoch:
        code = "planning_round_epoch_mismatch"
        raise StoreError(code)
    if record.stage is RoundStage.FINALIZED:
        return record
    if not record.armed:
        owner = text_cursor(
            database,
            """SELECT owner FROM outbox WHERE goal_id = ? AND operation_id = ?
               AND lease_until = ?""",
            (lease.goal_id, lease.operation_id, str(lease.expires_at)),
        ).fetchone()
        if owner is None or owner[0] != lease.owner:
            return record
    if record.armed and (record.feedback is None or not record.feedback.terminal):
        return record
    disposition, plan_revision, rejection = _disposition(database, record)
    changed = record.model_copy(
        update={
            "stage": RoundStage.FINALIZED,
            "revision": record.revision + 1,
            "disposition": disposition,
            "committed_plan_revision": plan_revision,
            "rejection_code": rejection,
        }
    )
    write_round(database, changed)
    _settle(database, changed)
    return changed
