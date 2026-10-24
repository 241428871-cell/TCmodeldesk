"""Human controls and original authority fence receipt recovery."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import continuation_clock
import dynamic_plan
import goal_controller
import goal_resume
import panel_server as panel
import stage_store
from goal_scan import ScanAction, decide
from plan_contracts import PlanPatch
from test_goal_action_receipts import GoalRun, proposal
from test_goal_action_receipts import run as run
from test_goal_resume import no_native, saved_actions, view


@pytest.mark.parametrize("action", ["stop", "pause", "hold"])
def test_human_control_cannot_be_cleared_by_saved_action_recovery(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    saved_actions(run)
    _ = continuation_clock.control(
        run.scope.project,
        run.scope.goal_id,
        action,
        {"kind": "unknown", "reason": "Human hold requires its own resolution"},
    )
    before = view(run)
    monkeypatch.setattr("goal_brain_runner.run", no_native)

    result = goal_controller.run_once(run.scope.project, run.scope.goal_id, panel, "new-service")

    assert result is None
    assert view(run) == before
    assert len(run.queued) == 1


@pytest.mark.parametrize("verdict", ["running", "unknown"])
def test_live_or_uninspectable_original_process_keeps_its_owner(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch, verdict: str
) -> None:
    saved_actions(run)
    before = view(run)

    def inspect(_pid: int, _birth: float) -> tuple[str, str]:
        return verdict, "Fixture"

    monkeypatch.setattr("goal_brain_runner.inspect_owned", inspect)
    monkeypatch.setattr("goal_brain_runner.run", no_native)

    result = goal_controller.run_once(run.scope.project, run.scope.goal_id, panel, "new-service")

    assert result is None
    assert view(run) == before


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE goal_graph_steps SET version=1 WHERE step='actions'",
        "UPDATE goal_graph_steps SET scope_hash='foreign' WHERE step='actions'",
        "UPDATE goal_graph_steps SET status='inflight' WHERE step='native'",
        "UPDATE goal_graph_steps SET result_json='{}' WHERE step='snapshot'",
        (
            "UPDATE goal_graph_steps SET result_json="
            "json_set(result_json,'$.action_receipts_version',0) WHERE step='snapshot'"
        ),
    ],
)
def test_old_foreign_or_incomplete_receipts_cannot_authorize_replay(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    saved_actions(run)
    with stage_store.connection(run.scope.project) as database:
        _ = database.execute(change)
    before = view(run)
    monkeypatch.setattr("goal_brain_runner.run", no_native)

    result = goal_controller.run_once(run.scope.project, run.scope.goal_id, panel, "new-service")

    assert result is None
    assert view(run) == before
    assert len(run.queued) == 1


def test_two_recovery_candidates_cannot_both_adopt_the_original_lease(run: GoalRun) -> None:
    saved_actions(run)
    first = goal_resume.candidate(run.scope.project, run.scope.goal_id, "service-a")
    second = goal_resume.candidate(run.scope.project, run.scope.goal_id, "service-b")
    assert first is not None
    assert second is not None
    assert goal_resume.adopt(first)

    adopted = goal_resume.adopt(second)

    assert adopted is False
    assert len(run.queued) == 1


def test_round_failure_cannot_replace_concurrent_human_hold(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved_actions(run, human_hold=True)
    before = view(run)
    monkeypatch.setattr("goal_brain_runner.run", no_native)

    result = goal_controller.run_once(run.scope.project, run.scope.goal_id, panel, "new-service")

    assert result is None
    assert view(run) == before


@pytest.mark.parametrize("with_plan", [True, False])
def test_intervening_plan_revision_cannot_change_original_batch_targets(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch, *, with_plan: bool
) -> None:
    original = PlanPatch.model_validate(proposal(run)["plan"])
    added = original.stages[0].model_copy(update={"key": "new-stage", "title": "New branch"})
    updated = original.model_copy(
        update={
            "base_revision": 1,
            "reason": "New information",
            "stages": [*original.stages, added],
            "authority_epoch": run.scope.epoch,
        }
    )

    def update() -> None:
        _ = dynamic_plan.revise_plan(run.scope.project, run.scope.goal_id, updated)

    saved_actions(run, with_plan=with_plan, after_actions=update)
    monkeypatch.setattr("goal_brain_runner.run", no_native)

    result = goal_controller.run_once(run.scope.project, run.scope.goal_id, panel, "new-service")

    assert result is not None
    assert view(run).active_intent_id == run.scope.operation_id
    assert view(run).last_result["status"] == "unknown"
    assert len(run.queued) == 1


def test_completed_goal_with_saved_actions_is_resumed_before_terminal_bookkeeping(
    run: GoalRun,
) -> None:
    saved_actions(run)

    action = decide(run.scope.project, run.scope.goal_id, "completed", "new-service")

    assert action is ScanAction.RUN


def test_adopted_live_lease_is_not_polled_as_another_round(run: GoalRun) -> None:
    saved_actions(run)
    claim = goal_resume.candidate(run.scope.project, run.scope.goal_id, "service-a")
    assert claim is not None
    assert goal_resume.adopt(claim)

    action = decide(run.scope.project, run.scope.goal_id, "active", "service-b")

    assert action is ScanAction.SKIP


def test_process_only_reconcile_cannot_discard_stopped_native_effects(run: GoalRun) -> None:
    saved_actions(run)
    _ = continuation_clock.control(run.scope.project, run.scope.goal_id, "stop")
    before = view(run)

    with pytest.raises(ValueError, match="仅退出证明不能核销"):
        _ = continuation_clock.control(
            run.scope.project,
            run.scope.goal_id,
            "reconcile",
            {
                "intent_id": run.scope.operation_id,
                "worker_exited": True,
                "evidence": "The fixture process has exited; effects remain to be reconciled",
            },
        )

    assert view(run) == before
