"""Reconciliation cannot reuse stale evidence, overwrite history or waive live workers."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from model_desk_control import host_resolution

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cli_worker
import continuation_clock
import dynamic_plan
import goal_resolution
import governance_api
import model_registry as registry
import panel_server as panel
from test_goal_action_receipts import run as run
from test_goal_resolution import cancelled_work, clock, decision
from test_goal_resume import saved_actions

if TYPE_CHECKING:
    from sqlite3 import Connection

    from test_goal_action_receipts import GoalRun

    from model_desk_control.host_resolution_models import (
        ResolutionCommit,
        ResolutionInput,
    )
    from model_desk_control.host_resolution_tasks import ProcessProbe


def test_reopening_a_processed_decision_preserves_current_stop(run: GoalRun) -> None:
    cancelled_work(run)
    issue = goal_resolution.inspect_goal(run.scope.project, run.scope.goal_id)
    request = decision(issue)
    original = goal_resolution.resolve_goal(run.scope.project, run.scope.goal_id, request)
    _ = continuation_clock.control(run.scope.project, run.scope.goal_id, "stop")
    before = clock(run)

    replayed = goal_resolution.resolve_goal(run.scope.project, run.scope.goal_id, request)

    assert replayed.replayed
    assert replayed.receipt == original.receipt
    assert replayed.clock == before


def test_late_different_notes_cannot_replace_the_original_human_decision(run: GoalRun) -> None:
    cancelled_work(run)
    issue = goal_resolution.inspect_goal(run.scope.project, run.scope.goal_id)
    request = decision(issue)
    _ = goal_resolution.resolve_goal(run.scope.project, run.scope.goal_id, request)
    before = clock(run)

    with pytest.raises(goal_resolution.ReconciliationError, match="另一份依据"):
        _ = goal_resolution.resolve_goal(
            run.scope.project, run.scope.goal_id, {**request, "notes": "A different late statement"}
        )

    assert clock(run) == before


@pytest.mark.parametrize("change", ["task", "criteria"])
def test_changed_task_or_criteria_requires_fresh_human_observation(
    run: GoalRun, change: str
) -> None:
    cancelled_work(run)
    issue = goal_resolution.inspect_goal(run.scope.project, run.scope.goal_id)
    if change == "task":
        _ = registry.patch_task(registry.task_dir(run.queued[0]), phase="New recorded information")
    else:
        _ = dynamic_plan.configure_goal(
            run.scope.project, run.scope.goal_id, {"criteria": ["Updated human criterion"]}
        )
    before = clock(run)

    with pytest.raises(goal_resolution.ReconciliationError, match="已变化"):
        _ = goal_resolution.resolve_goal(run.scope.project, run.scope.goal_id, decision(issue))

    assert clock(run) == before


@pytest.mark.parametrize("state", ["running", "unknown"])
def test_notes_do_not_replace_original_process_exit_proof(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch, state: Literal["running", "unknown"]
) -> None:
    cancelled_work(run)

    def probe(_pid: int, _birth: float) -> Literal["running", "unknown"]:
        return state

    monkeypatch.setattr("goal_resolution._probe", probe)
    issue = goal_resolution.inspect_goal(run.scope.project, run.scope.goal_id)
    before = clock(run)

    with pytest.raises(goal_resolution.ReconciliationError, match="仍有工作者"):
        _ = goal_resolution.resolve_goal(run.scope.project, run.scope.goal_id, decision(issue))

    assert not issue.can_resolve
    assert clock(run) == before


def test_terminal_task_with_live_native_child_still_blocks_resolution(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    cancelled_work(run)
    _ = registry.patch_task(
        registry.task_dir(run.queued[0]),
        process_pid=77,
        process_birth=1234.0,
        native_output_capture=1,
    )

    def probe(pid: int, _birth: float) -> Literal["running", "exited"]:
        return "running" if pid == 77 else "exited"

    monkeypatch.setattr("goal_resolution._probe", probe)

    issue = goal_resolution.inspect_goal(run.scope.project, run.scope.goal_id)

    assert not issue.can_resolve
    assert issue.facts.tasks[0].process_state == "running"


def test_resolution_rolls_back_intent_and_clock_together(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    cancelled_work(run)
    issue = goal_resolution.inspect_goal(run.scope.project, run.scope.goal_id)
    before = clock(run)
    actual = host_resolution.resolve

    def fail(
        database: Connection,
        project: Path,
        goal_id: str,
        request: ResolutionInput,
        probe: ProcessProbe,
    ) -> ResolutionCommit:
        _ = actual(database, project, goal_id, request, probe)
        reason = "Fixture fault before the project transaction commits"
        raise OSError(reason)

    monkeypatch.setattr(host_resolution, "resolve", fail)

    with pytest.raises(OSError, match="before the project transaction commits"):
        _ = goal_resolution.resolve_goal(run.scope.project, run.scope.goal_id, decision(issue))

    assert clock(run) == before
    assert goal_resolution.inspect_goal(run.scope.project, run.scope.goal_id).can_resolve


def test_human_route_cannot_use_another_project_body(run: GoalRun, tmp_path: Path) -> None:
    cancelled_work(run)
    issue = goal_resolution.inspect_goal(run.scope.project, run.scope.goal_id)
    other = tmp_path / "other"
    other.mkdir()
    before = clock(run)

    with pytest.raises(PermissionError):
        _ = governance_api.post(
            "/api/governance/goals/" + run.scope.goal_id + "/reconcile",
            {**decision(issue), "project": str(other)},
            {"mode": "project", "project": run.scope.project},
            panel,
        )

    assert clock(run) == before
    assert not (other / ".model-desk").exists()


@pytest.mark.parametrize("field", ["actor", "quality_accepted"])
def test_client_cannot_supply_acceptance_or_provenance_fields(run: GoalRun, field: str) -> None:
    cancelled_work(run)
    issue = goal_resolution.inspect_goal(run.scope.project, run.scope.goal_id)
    before = clock(run)

    with pytest.raises(ValidationError):
        _ = goal_resolution.resolve_goal(
            run.scope.project, run.scope.goal_id, {**decision(issue), field: True}
        )

    assert clock(run) == before


def test_stale_native_worker_cannot_start_after_human_disposition(run: GoalRun) -> None:
    saved_actions(run)
    task = registry.task_dir(run.queued[0])
    stale: cli_worker.NativeTask = {
        "engine": "codex_cli",
        "kind": "chat",
        "project": run.scope.project,
        "files": [],
        "timeout": 30,
        "goal_id": run.scope.goal_id,
        "authority_epoch": run.scope.epoch,
        "goal_contract_revision": 1,
        "automatic_goal": True,
    }
    _ = panel.cancel_task(run.queued[0], reason="Human cancellation", actor="human")
    issue = goal_resolution.inspect_goal(run.scope.project, run.scope.goal_id)
    _ = goal_resolution.resolve_goal(run.scope.project, run.scope.goal_id, decision(issue))
    # Simulate loss of the file flag: the original epoch must still fence the worker.
    (task / "cancelled.flag").unlink()
    with patch.object(cli_worker, "command", return_value=[sys.executable, "-c", "pass"]):
        with (
            patch.object(
                subprocess,
                "Popen",
                side_effect=AssertionError("A retired task attempted to spawn a native process"),
            ) as spawn,
            pytest.raises(ValueError, match="授权代次"),
        ):
            cli_worker.run_task(task, stale, {"provider": "codex_cli", "model": "fixture-model"})
        spawn.assert_not_called()


@pytest.mark.parametrize(
    "fields",
    [
        {"cli_launch_protocol": None},
        {"native_launch_state": "armed"},
    ],
)
def test_missing_or_armed_launch_receipt_is_not_guessed_unstarted(
    run: GoalRun, fields: dict[str, str | None]
) -> None:
    cancelled_work(run)
    _ = registry.patch_task(registry.task_dir(run.queued[0]), **fields)

    issue = goal_resolution.inspect_goal(run.scope.project, run.scope.goal_id)

    assert not issue.can_resolve
    assert issue.facts.tasks[0].process_state == "unknown"
