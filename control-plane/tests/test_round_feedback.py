"""Duplicate and stale runner observations cannot rewrite the native operation history."""

from pathlib import Path

import pytest

from model_desk_control.database import StoreError
from model_desk_control.plans import PlanPatch
from model_desk_control.round_models import FeedbackKind, RoundDisposition, RoundStage
from tests.domain_fixtures import task
from tests.round_fixtures import feedback_for, prepared_round


def test_duplicate_and_older_feedback_do_not_regress_current_observation(tmp_path: Path) -> None:
    _, lease, rounds = prepared_round(tmp_path)
    _, record = rounds.choose(lease, now=101)
    running = feedback_for(record.request, FeedbackKind.RUNNING, 5)
    current = rounds.observe(lease.goal_id, running)
    assert rounds.observe(lease.goal_id, running) == current
    assert (
        rounds.observe(lease.goal_id, feedback_for(record.request, FeedbackKind.UNKNOWN, 2))
        == current
    )


@pytest.mark.parametrize("same_receipt", [True, False])
def test_conflicting_receipt_or_sequence_is_rejected(tmp_path: Path, *, same_receipt: bool) -> None:
    _, lease, rounds = prepared_round(tmp_path)
    _, record = rounds.choose(lease, now=101)
    running = feedback_for(record.request, FeedbackKind.RUNNING)
    _ = rounds.observe(lease.goal_id, running)
    changed = running.model_copy(
        update={"reason": "A conflicting body"} if same_receipt else {"receipt_id": "other-id"}
    )
    with pytest.raises(StoreError, match="runner_receipt_conflict"):
        _ = rounds.observe(lease.goal_id, changed)


def test_transport_unknown_cannot_undo_a_known_terminal_result(tmp_path: Path) -> None:
    _, lease, rounds = prepared_round(tmp_path)
    _, record = rounds.choose(lease, now=101)
    result = rounds.observe(lease.goal_id, feedback_for(record.request))
    assert (
        rounds.observe(lease.goal_id, feedback_for(record.request, FeedbackKind.UNKNOWN, 2))
        == result
    )
    with pytest.raises(StoreError, match="terminal_runner_receipt_conflict"):
        _ = rounds.observe(lease.goal_id, feedback_for(record.request, FeedbackKind.FAILED, 3))


def test_unarmed_round_rejects_unsolicited_native_receipt(tmp_path: Path) -> None:
    _, lease, rounds = prepared_round(tmp_path)
    request = rounds.read(lease.goal_id, lease.operation_id).request
    with pytest.raises(StoreError, match="round_not_armed"):
        _ = rounds.observe(lease.goal_id, feedback_for(request))
    assert rounds.read(lease.goal_id, lease.operation_id).stage is RoundStage.PREPARED


def test_invalid_native_dag_result_settles_without_partial_plan_write(tmp_path: Path) -> None:
    controls, lease, rounds = prepared_round(tmp_path)
    _, record = rounds.choose(lease, now=101)
    feedback = feedback_for(record.request).model_copy(
        update={
            "patch": PlanPatch(
                base_revision=1,
                reason="Invalid graph from the native brain",
                added=(task("bad", dependencies=("missing",)),),
            )
        }
    )
    _ = rounds.observe(lease.goal_id, feedback)
    settled = rounds.finalize(lease)
    assert settled.disposition is RoundDisposition.REPLAN_REQUIRED
    assert settled.rejection_code == "unknown_or_superseded_dependency"
    assert not controls.read(lease.goal_id).busy
