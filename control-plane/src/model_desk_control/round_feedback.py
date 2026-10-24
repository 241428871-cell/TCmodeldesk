"""Reconciliation stores observations without restarting or inventing native completion."""

from sqlite3 import Connection
from typing import assert_never

from model_desk_control.database import StoreError
from model_desk_control.round_models import BrainFeedback, FeedbackKind, PlanningRound, RoundStage
from model_desk_control.round_records import record_feedback, require_round, write_round


def _stage(feedback: BrainFeedback) -> RoundStage:
    match feedback.kind:
        case FeedbackKind.RUNNING:
            return RoundStage.IN_FLIGHT
        case FeedbackKind.UNKNOWN:
            return RoundStage.UNKNOWN
        case FeedbackKind.RESULT:
            return RoundStage.RESULT
        case FeedbackKind.FAILED:
            return RoundStage.FAILED
        case FeedbackKind.NOT_STARTED:
            return RoundStage.NOT_STARTED
        case _:
            assert_never(feedback.kind)


def accept_feedback(database: Connection, goal_id: str, feedback: BrainFeedback) -> PlanningRound:
    """Archive valid observations even after STOP, but never turn them into new authority."""
    record = require_round(database, goal_id, feedback.operation_id)
    if not record.armed:
        code = "round_not_armed"
        raise StoreError(code)
    if not record_feedback(database, feedback):
        return record
    prior = record.feedback
    if prior is not None:
        if feedback.sequence < prior.sequence:
            return record
        if prior.terminal:
            if feedback.terminal and (feedback.kind != prior.kind or feedback.patch != prior.patch):
                code = "terminal_runner_receipt_conflict"
                raise StoreError(code)
            return record
    changed = record.model_copy(
        update={"stage": _stage(feedback), "feedback": feedback, "revision": record.revision + 1}
    )
    write_round(database, changed)
    return changed
