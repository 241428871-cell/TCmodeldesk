"""Event delivery survives source/claim failures without revoking current user control."""

from __future__ import annotations

import json
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import JsonValue, TypeAdapter

from model_desk_control.database import text_cursor
from model_desk_control.host_events import EventError, pending

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import continuation_clock
import goal_events
import model_registry as registry
import stage_store
from test_goal_action_receipts import GoalRun, apply, proposal
from test_goal_action_receipts import run as run
from test_goal_events import claim_event, completed_task, finish_initial
from test_goal_resume import view

if TYPE_CHECKING:
    from goal_brain_contract import Record


def claim(run: GoalRun) -> Record | None:
    result = continuation_clock.claim_due(
        run.scope.project, run.scope.goal_id, "driver", host_protocol="graph_v2"
    )
    return TypeAdapter(dict[str, JsonValue]).validate_json(json.dumps(result)) if result else None


def inbox(run: GoalRun) -> tuple[str, ...]:
    with stage_store.connection(run.scope.project) as database:
        return tuple(
            event.identity
            for event in pending(database, run.scope.project, run.scope.goal_id, run.scope.epoch)
        )


def test_two_drivers_claim_one_observation_once(run: GoalRun) -> None:
    _ = completed_task(run)
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = tuple(workers.map(claim, (run, run)))
    assert sum(result is not None for result in results) == 1
    assert inbox(run) == ()


def test_clock_write_failure_rolls_back_event_binding(run: GoalRun) -> None:
    _ = completed_task(run)
    before = view(run)
    original = inbox(run)
    with stage_store.connection(run.scope.project) as database:
        _ = database.execute("""CREATE TRIGGER fail_clock BEFORE UPDATE ON continuation_clocks
        BEGIN SELECT RAISE(ABORT,'fixture clock failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="fixture clock failure"):
        _ = claim(run)
    assert inbox(run) == original
    assert view(run) == before
    with stage_store.connection(run.scope.project) as database:
        _ = database.execute("DROP TRIGGER fail_clock")
    assert claim_event(run)["event_ids"] == list(original)


def test_source_failure_does_not_commit_stage_without_event(run: GoalRun) -> None:
    result = apply(run, proposal(run))
    finish_initial(run)
    task_id = result.dispatched_task_ids[0]
    _ = registry.patch_task(registry.task_dir(task_id), status="completed", goal_round_id="foreign")
    with pytest.raises(EventError, match="event_task_round_identity"):
        _ = stage_store.sync_project_goal(run.scope.project, run.scope.goal_id)
    with stage_store.connection(run.scope.project) as database:
        row = text_cursor(
            database, "SELECT status FROM stages WHERE goal_id=?", (run.scope.goal_id,)
        ).fetchone()
    assert row is not None
    assert row[0] == "dispatched"
    _ = registry.patch_task(registry.task_dir(task_id), goal_round_id=run.scope.operation_id)
    _ = stage_store.sync_project_goal(run.scope.project, run.scope.goal_id)
    assert claim_event(run)["trigger_kind"] == "event"


def test_same_terminal_result_and_quiet_scans_cannot_generate_new_work(run: GoalRun) -> None:
    _ = completed_task(run)
    original = inbox(run)
    for _ in range(3):
        _ = stage_store.sync_project_goal(run.scope.project, run.scope.goal_id)
        assert inbox(run) == original
    assert len(original) == 1
    assert claim(run) is not None
    assert claim(run) is None


def test_current_round_retains_ownership_until_it_finishes(run: GoalRun) -> None:
    result = apply(run, proposal(run))
    task_id = result.dispatched_task_ids[0]
    _ = registry.patch_task(registry.task_dir(task_id), status="completed")
    _ = stage_store.sync_project_goal(run.scope.project, run.scope.goal_id)
    assert claim(run) is None
    finish_initial(run)
    assert claim_event(run)["trigger_kind"] == "event"


def test_stop_then_resume_does_not_reactivate_old_epoch_event(run: GoalRun) -> None:
    _ = completed_task(run)
    _ = continuation_clock.control(run.scope.project, run.scope.goal_id, "stop")
    _ = continuation_clock.control(run.scope.project, run.scope.goal_id, "resume")
    assert claim(run) is None
    assert len(inbox(run)) == 1


def test_backoff_cannot_be_bypassed_by_pending_task_event(run: GoalRun) -> None:
    _ = completed_task(run)
    _ = continuation_clock.control(run.scope.project, run.scope.goal_id, "backoff")
    before = view(run)
    assert claim(run) is None
    assert view(run) == before


def test_resource_recovery_preserves_existing_due_time(run: GoalRun) -> None:
    _ = completed_task(run)
    _ = continuation_clock.control(
        run.scope.project,
        run.scope.goal_id,
        "hold",
        {"kind": "resource", "reason": "Resource unavailable", "guard_id": "resource_1"},
    )
    _ = continuation_clock.control(
        run.scope.project,
        run.scope.goal_id,
        "clear_hold",
        {"guard_id": "resource_1", "evidence": "Resource available"},
    )
    before = view(run)
    assert claim(run) is None
    assert view(run) == before


def test_event_record_with_foreign_identity_never_starts_a_round(run: GoalRun) -> None:
    _ = completed_task(run)
    with stage_store.connection(run.scope.project) as database:
        _ = database.execute("UPDATE goal_events SET data=json_set(data,'$.goal_id','foreign')")
    before = view(run)
    with pytest.raises(EventError, match="event_record_identity"):
        _ = claim(run)
    assert view(run) == before


def test_completion_callback_uses_project_store_without_global_locator(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = apply(run, proposal(run))
    finish_initial(run)
    task_id = result.dispatched_task_ids[0]
    state = registry.patch_task(registry.task_dir(task_id), status="completed")

    def no_fallback(_goal_id: str) -> str:
        pytest.fail("Managed completion must not consult another store")

    monkeypatch.setattr(stage_store, "_project_of_goal", no_fallback)
    goal_events.sync_task(state)
    assert claim_event(run)["trigger_kind"] == "event"
