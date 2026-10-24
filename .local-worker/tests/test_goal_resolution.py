"""Human disposition uses actual scoped tasks, cancellation records and project transactions."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import JsonValue, TypeAdapter

from model_desk_control.database import text_cursor

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import continuation_clock
import goal_resolution
import model_registry as registry
import panel_server as panel
import stage_store
from test_goal_action_receipts import run as run
from test_goal_resume import saved_actions

if TYPE_CHECKING:
    from test_goal_action_receipts import GoalRun

    from model_desk_control.host_resolution_models import ResolutionView

type Record = dict[str, JsonValue]


def decision(issue: ResolutionView) -> Record:
    return {
        "intent_id": issue.facts.scope.operation_id,
        "authority_epoch": issue.facts.authority_epoch,
        "subject_hash": issue.subject_hash,
        "action": "retain_state",
        "reviewed_workspace": True,
        "notes": "Reviewed workspace and original receipts. Retain files for fresh brain review.",
    }


def clock(run: GoalRun) -> Record:
    return TypeAdapter(Record).validate_python(
        continuation_clock.snapshot(run.scope.project, run.scope.goal_id)
    )


def cancelled_work(run: GoalRun) -> None:
    saved_actions(run)
    _ = panel.cancel_task(
        run.queued[0], reason="Human inspected this original queued task", actor="human"
    )


def test_queued_work_prevents_disposition_even_with_human_notes(run: GoalRun) -> None:
    saved_actions(run)
    issue = goal_resolution.inspect_goal(run.scope.project, run.scope.goal_id)
    before = clock(run)

    with pytest.raises(goal_resolution.ReconciliationError, match="仍有工作者"):
        _ = goal_resolution.resolve_goal(run.scope.project, run.scope.goal_id, decision(issue))

    assert issue.can_resolve is False
    assert clock(run) == before


def test_human_retains_state_without_accepting_work_or_resetting_schedule(run: GoalRun) -> None:
    cancelled_work(run)
    issue = goal_resolution.inspect_goal(run.scope.project, run.scope.goal_id)
    assert issue.can_resolve, issue.blockers
    before = clock(run)
    task_before = registry.read_task(registry.task_dir(run.queued[0]))

    result = goal_resolution.resolve_goal(run.scope.project, run.scope.goal_id, decision(issue))

    assert result.receipt.actor == "human"
    assert result.receipt.quality_accepted is False
    assert result.clock["active_intent_id"] == ""
    assert result.clock["authority_epoch"] == run.scope.epoch + 1
    for key in (
        "interval_seconds",
        "next_due_at",
        "user_intent",
        "enabled",
        "obligations",
        "brain_model_id",
        "native_session_id",
    ):
        assert result.clock[key] == before[key]
    assert registry.read_task(registry.task_dir(run.queued[0])) == task_before
    with stage_store.connection(run.scope.project) as database:
        row = text_cursor(
            database, "SELECT status FROM stages WHERE goal_id=?", (run.scope.goal_id,)
        ).fetchone()
        assert row is not None
        assert row[0] == "dispatched"


@pytest.mark.parametrize("action", ["pause", "stop"])
def test_human_disposition_preserves_explicit_pause_or_stop(run: GoalRun, action: str) -> None:
    cancelled_work(run)
    _ = continuation_clock.control(run.scope.project, run.scope.goal_id, action)
    before = clock(run)
    issue = goal_resolution.inspect_goal(run.scope.project, run.scope.goal_id)

    result = goal_resolution.resolve_goal(run.scope.project, run.scope.goal_id, decision(issue))

    assert result.clock["enabled"] is False
    assert result.clock["user_intent"] == before["user_intent"]
    assert result.clock["next_due_at"] == before["next_due_at"]
    assert result.receipt.authority_epoch == before["authority_epoch"]
