"""Resume actual saved graph actions without starting another native process."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

import pytest
from pydantic import BaseModel, ConfigDict

from model_desk_control.host_flow import (
    HostApplied,
    HostCallbacks,
    HostNativeResult,
    HostProfile,
    HostSnapshot,
)
from model_desk_control.native_commands import NativeMode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import continuation_clock
import continuation_rounds
import dynamic_plan
import goal_actions
import goal_controller
import goal_resume
import model_registry as registry
import panel_server as panel
import stage_store
from goal_graph_bridge import GoalGraphHost
from plan_contracts import PlanPatch
from test_goal_action_receipts import GoalRun, proposal
from test_goal_action_receipts import run as run

if TYPE_CHECKING:
    from collections.abc import Callable

    from goal_brain_contract import Record


class ClockView(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="ignore")
    authority_epoch: int
    active_intent_id: str
    interval_seconds: int
    next_due_at: str
    hold: dict[str, str]
    last_result: dict[str, str | bool | list[str]]


def view(run: GoalRun) -> ClockView:
    return ClockView.model_validate_json(
        json.dumps(continuation_clock.snapshot(run.scope.project, run.scope.goal_id))
    )


def saved_actions(
    run: GoalRun,
    *,
    human_hold: bool = False,
    with_plan: bool = True,
    after_actions: Callable[[], None] | None = None,
    on_applied: Callable[[HostApplied], None] | None = None,
) -> None:
    """Lose a phase response after actual plan commit and actual task creation."""
    _ = continuation_clock.record_runner(
        run.scope.project,
        run.scope.goal_id,
        run.scope.operation_id,
        run.scope.epoch,
        900001,
        1234.0,
    )
    value = proposal(run)
    if not with_plan:
        _ = dynamic_plan.revise_plan(
            run.scope.project, run.scope.goal_id, PlanPatch.model_validate(value["plan"])
        )
        value["plan"] = None
    snapshot = HostSnapshot(
        context={"engineering_policy": {}},
        engine="codex_cli",
        model="fixture-model",
        runtime=HostProfile(),
        directory=run.scope.project,
        session_id="",
        permission_mode=NativeMode.AUTO,
        contract_revision=1,
        action_receipts_version=2,
        settle_receipts_version=2,
        plan_revision=0 if with_plan else 1,
    )
    result = HostNativeResult(
        actions_json=json.dumps(value),
        native_session_id="saved-native-session",
        receipt=run.scope.project + "/saved-actions.json",
    )

    def actions() -> str:
        applied = goal_actions.apply(
            run.scope.project,
            run.scope.goal_id,
            run.scope.epoch,
            value,
            panel,
            {},
            result.receipt,
            contract_revision=snapshot.contract_revision,
            scope=run.scope,
            plan_revision=snapshot.plan_revision,
        )
        if on_applied is not None:
            on_applied(HostApplied.model_validate_json(json.dumps(applied)))
        if human_hold:
            _ = continuation_clock.control(
                run.scope.project,
                run.scope.goal_id,
                "hold",
                {"kind": "unknown", "reason": "Human inspection is still in progress"},
            )
        if after_actions is not None:
            after_actions()
        reason = "Fixture lost the action phase response after actual task creation"
        raise OSError(reason)

    host = GoalGraphHost(
        run.scope,
        HostCallbacks(
            snapshot.model_dump_json,
            result.model_dump_json,
            actions,
            lambda: "{}",
        ),
        replay_actions=True,
    )
    with pytest.raises(OSError, match="lost the action phase response"):
        host.run()
    _ = continuation_clock.finish_round(
        run.scope.project,
        run.scope.goal_id,
        run.scope.operation_id,
        run.scope.epoch,
        {"status": "unknown", "summary": "Fixture action response lost", "retryable": False},
    )


def no_native(*_args: Record, **_kwargs: Record) -> None:
    pytest.fail("Recovery must use the existing native phase receipt")


@pytest.mark.parametrize("phase_saved", [True, False])
def test_original_actions_resume_without_native_or_duplicate_task(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch, *, phase_saved: bool
) -> None:
    saved_actions(run)
    if not phase_saved:
        with stage_store.connection(run.scope.project) as database:
            _ = database.execute("DELETE FROM goal_graph_steps WHERE step='actions'")
    before = view(run)
    monkeypatch.setattr("goal_brain_runner.run", no_native)

    _ = goal_controller.run_once(run.scope.project, run.scope.goal_id, panel, "new-service")

    after = view(run)
    assert after.active_intent_id == "", after.last_result
    assert not after.hold
    assert after.last_result["status"] == "completed"
    assert after.last_result["dispatched_task_ids"] == run.queued
    assert len(run.queued) == 1
    assert (after.interval_seconds, after.next_due_at, after.authority_epoch) == (
        before.interval_seconds,
        before.next_due_at,
        before.authority_epoch,
    )


def test_unattributed_external_effect_stays_owned_after_one_recovery(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved_actions(run)
    _ = registry.patch_task(registry.task_dir(run.queued[0]), goal_round_id="foreign")
    with stage_store.connection(run.scope.project) as database:
        _ = database.execute(
            "UPDATE brain_requests SET state='unknown',receipt='{}' WHERE kind='dispatch'"
        )
    monkeypatch.setattr("goal_brain_runner.run", no_native)

    result = goal_controller.run_once(run.scope.project, run.scope.goal_id, panel, "new-service")

    assert result is not None
    assert view(run).active_intent_id == run.scope.operation_id
    assert view(run).last_result["status"] == "unknown"
    assert goal_resume.candidate(run.scope.project, run.scope.goal_id, "another-service") is None
    assert len(run.queued) == 1


@pytest.mark.parametrize("shutdown", [True, False])
def test_service_shutdown_or_expired_lease_resumes_existing_receipts(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch, *, shutdown: bool
) -> None:
    saved_actions(run)
    before = view(run)
    with stage_store.connection(run.scope.project) as database:
        _ = database.execute(
            "UPDATE continuation_clocks SET data=json_set(data,'$.hold',json('{}'))"
        )
        _ = database.execute(
            """UPDATE continuation_intents SET status='started',
               data=json_set(data,'$.status','started','$.lease_until',?)""",
            ("2099-01-01T00:00:00+00:00" if shutdown else "2000-01-01T00:00:00+00:00",),
        )
    if shutdown:
        _ = continuation_rounds.suspend_owned(
            run.scope.project, run.scope.goal_id, run.scope.owner_id
        )
    monkeypatch.setattr("goal_brain_runner.run", no_native)

    _ = goal_controller.run_once(run.scope.project, run.scope.goal_id, panel, "new-service")

    after = view(run)
    assert not after.hold
    assert after.active_intent_id == ""
    assert len(run.queued) == 1
    assert (after.interval_seconds, after.next_due_at, after.authority_epoch) == (
        before.interval_seconds,
        before.next_due_at,
        before.authority_epoch,
    )
