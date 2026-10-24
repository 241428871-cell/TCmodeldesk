"""Real domain transactions fence starts and settle only evidenced terminal operations."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from model_desk_control.control import ContractError, UserIntent
from model_desk_control.database import StoreError
from model_desk_control.outbox import OutboxStore
from model_desk_control.plan_store import PlanStore
from model_desk_control.round_models import FeedbackKind, RoundAction, RoundDisposition, RoundStage
from model_desk_control.round_store import RoundStore
from model_desk_control.store import ControlStore
from tests.round_fixtures import feedback_for, prepared_round
from tests.test_plans_persistence import add_command


def test_competing_controllers_authorize_only_one_first_start(tmp_path: Path) -> None:
    controls, lease, _ = prepared_round(tmp_path)

    def compete(_: str) -> RoundAction:
        action, _record = RoundStore(ControlStore(controls.path)).choose(lease, now=101)
        return action

    with ThreadPoolExecutor(max_workers=2) as pool:
        actions = list(pool.map(compete, ("a", "b")))
    assert sorted(actions) == [RoundAction.INSPECT, RoundAction.START]


def test_terminal_round_commits_plan_and_releases_busy_without_closing_clock(
    tmp_path: Path,
) -> None:
    controls, lease, rounds = prepared_round(tmp_path)
    _, record = rounds.choose(lease, now=101)
    _ = rounds.observe(lease.goal_id, feedback_for(record.request))
    settled = rounds.finalize(lease)
    assert settled.disposition is RoundDisposition.APPLIED
    assert settled.committed_plan_revision == 2
    state = controls.read(lease.goal_id)
    assert not state.busy
    assert state.due_at == 1900
    assert not state.closed
    assert not state.goal_complete
    assert not OutboxStore(controls).authorize(lease, now=102)
    assert rounds.finalize(lease) == settled
    assert controls.read(lease.goal_id) == state
    with pytest.raises(ContractError, match="not_eligible"):
        _ = controls.claim(
            goal_id=lease.goal_id, request_id="too-soon", epoch=0, revision=state.revision, now=1899
        )
    assert controls.claim(
        goal_id=lease.goal_id, request_id="next-due", epoch=0, revision=state.revision, now=1900
    ).busy


def test_unknown_round_keeps_busy_and_never_rearms_after_expiry(tmp_path: Path) -> None:
    controls, lease, rounds = prepared_round(tmp_path)
    _, record = rounds.choose(lease, now=101)
    _ = rounds.observe(lease.goal_id, feedback_for(record.request, FeedbackKind.UNKNOWN))
    unresolved = rounds.finalize(lease)
    assert unresolved.stage is RoundStage.UNKNOWN
    assert controls.read(lease.goal_id).busy
    assert OutboxStore(controls).expire(now=130) == 1
    action, _ = rounds.choose(lease, now=131)
    assert action is RoundAction.INSPECT
    assert (
        OutboxStore(controls).take(
            goal_id=lease.goal_id, owner="replacement", now=131, lease_seconds=30
        )
        is None
    )


def test_positive_not_started_proof_releases_original_intent_without_resending(
    tmp_path: Path,
) -> None:
    controls, lease, rounds = prepared_round(tmp_path)
    _, record = rounds.choose(lease, now=101)
    _ = rounds.observe(lease.goal_id, feedback_for(record.request, FeedbackKind.UNKNOWN))
    _ = rounds.observe(lease.goal_id, feedback_for(record.request, FeedbackKind.NOT_STARTED, 2))
    settled = rounds.finalize(lease)
    assert settled.disposition is RoundDisposition.NOT_STARTED
    assert not controls.read(lease.goal_id).busy
    assert controls.read(lease.goal_id).due_at == 1900
    assert rounds.choose(lease, now=102)[0] is RoundAction.NONE


def test_late_result_after_stop_is_archived_without_applying_plan_or_reviving_clock(
    tmp_path: Path,
) -> None:
    controls, lease, rounds = prepared_round(tmp_path)
    _, record = rounds.choose(lease, now=101)
    stopped = controls.stop(goal_id=lease.goal_id, request_id="stop")
    assert stopped.busy
    _ = rounds.observe(lease.goal_id, feedback_for(record.request))
    settled = rounds.finalize(lease)
    assert settled.disposition is RoundDisposition.REVOKED
    assert PlanStore(controls).read(lease.goal_id).revision == 1
    state = controls.read(lease.goal_id)
    assert state.intent is UserIntent.STOP
    assert state.epoch == stopped.epoch
    assert state.due_at is None
    assert not state.busy


def test_never_armed_preparation_can_be_cancelled_using_domain_evidence(tmp_path: Path) -> None:
    controls, lease, rounds = prepared_round(tmp_path)
    _ = controls.stop(goal_id=lease.goal_id, request_id="stop")
    assert rounds.choose(lease, now=102)[0] is RoundAction.REVOKED
    record = rounds.finalize(lease)
    assert not record.armed
    assert record.feedback is None
    assert record.disposition is RoundDisposition.REVOKED
    assert not controls.read(lease.goal_id).busy


def test_other_replan_before_result_is_preserved_and_round_requests_fresh_planning(
    tmp_path: Path,
) -> None:
    controls, lease, rounds = prepared_round(tmp_path)
    _, record = rounds.choose(lease, now=101)
    _ = PlanStore(controls).patch(
        add_command("external").model_copy(update={"control_revision": 1})
    )
    _ = rounds.observe(lease.goal_id, feedback_for(record.request))
    settled = rounds.finalize(lease)
    assert settled.disposition is RoundDisposition.REPLAN_REQUIRED
    assert settled.rejection_code == "plan_revision_conflict"
    assert PlanStore(controls).read(lease.goal_id).tasks[-1].task_id == "external"
    assert not controls.read(lease.goal_id).busy


def test_replan_before_arm_prevents_start_from_obsolete_input(tmp_path: Path) -> None:
    controls, lease, rounds = prepared_round(tmp_path)
    _ = PlanStore(controls).patch(
        add_command("external").model_copy(update={"control_revision": 1})
    )
    assert rounds.choose(lease, now=101)[0] is RoundAction.REVOKED
    assert rounds.read(lease.goal_id, lease.operation_id).request.plan.revision == 1


def test_round_reads_require_exact_goal_scope(tmp_path: Path) -> None:
    _, lease, rounds = prepared_round(tmp_path)
    with pytest.raises(StoreError, match="planning_round_not_found"):
        _ = rounds.read("other/goal", lease.operation_id)
    with pytest.raises(StoreError, match="planning_round_epoch_mismatch"):
        _ = rounds.choose(lease.model_copy(update={"epoch": 7}), now=101)
