"""Original settlement commits and graph markers share the real project transaction."""

from __future__ import annotations

import sys
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import JsonValue, TypeAdapter

from model_desk_control import host_settlement
from model_desk_control.database import text_cursor

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import continuation_clock
import goal_controller
import goal_settlement
import panel_server as panel
import stage_store
from test_goal_action_receipts import run as run
from test_goal_resume import no_native, saved_actions, view

if TYPE_CHECKING:
    from sqlite3 import Connection

    from test_goal_action_receipts import GoalRun

    from model_desk_control.host_flow import HostApplied
    from model_desk_control.host_settlement import SettlementRequest

type Record = dict[str, JsonValue]


def pending_settlement(run: GoalRun) -> HostApplied:
    """Inject a crash after an actual action result, before its original settlement."""
    outcomes: list[HostApplied] = []
    saved_actions(run, on_applied=outcomes.append)
    outcome = outcomes[0]
    with stage_store.connection(run.scope.project) as database:
        _ = database.execute(
            "UPDATE goal_graph_steps SET status='done',result_json=? WHERE step='actions'",
            (outcome.model_dump_json(),),
        )
        _ = database.execute(
            "INSERT INTO goal_graph_steps VALUES (?,'settle',?,2,'inflight',NULL)",
            (run.scope.operation_id, sha256(run.scope.model_dump_json().encode()).hexdigest()),
        )
    return outcome


def test_original_inflight_settlement_resumes_without_native_or_action_replay(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = pending_settlement(run)
    before = view(run)
    monkeypatch.setattr("goal_brain_runner.run", no_native)
    monkeypatch.setattr("goal_controller.apply", no_native)

    _ = goal_controller.run_once(run.scope.project, run.scope.goal_id, panel, "new-service")

    after = view(run)
    assert after.active_intent_id == ""
    assert after.last_result["status"] == "completed"
    assert after.last_result["dispatched_task_ids"] == run.queued
    assert after.next_due_at == before.next_due_at
    assert len(run.queued) == 1
    with stage_store.connection(run.scope.project) as database:
        row = text_cursor(
            database, "SELECT status FROM goal_graph_steps WHERE step='settle'"
        ).fetchone()
        assert row is not None
        assert row[0] == "done"


def test_failure_after_marker_write_rolls_back_both_domain_and_graph_settlement(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = pending_settlement(run)
    actual = host_settlement.commit

    def fail_commit(database: Connection, request: SettlementRequest) -> None:
        actual(database, request)
        reason = "Fixture failure before the original transaction commits"
        raise OSError(reason)

    monkeypatch.setattr(host_settlement, "commit", fail_commit)
    monkeypatch.setattr("goal_brain_runner.run", no_native)

    _ = goal_controller.run_once(run.scope.project, run.scope.goal_id, panel, "new-service")

    assert view(run).active_intent_id == run.scope.operation_id
    assert view(run).last_result["status"] == "unknown"
    with stage_store.connection(run.scope.project) as database:
        row = text_cursor(
            database, "SELECT status FROM goal_graph_steps WHERE step='settle'"
        ).fetchone()
        assert row is not None
        assert row[0] == "inflight"
    assert len(run.queued) == 1


def test_lost_committed_settlement_response_keeps_the_completed_original_result(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = pending_settlement(run)
    actual = continuation_clock.finish_round

    def lose_response(  # noqa: PLR0913 - Match the existing continuation completion hook.
        project: str,
        goal_id: str,
        intent_id: str,
        epoch: int,
        result: Record,
        *,
        settlement: SettlementRequest | None = None,
    ) -> Record:
        completed = actual(project, goal_id, intent_id, epoch, result, settlement=settlement)
        if settlement is not None:
            reason = "Fixture lost response after the original domain transaction committed"
            raise OSError(reason)
        return completed

    monkeypatch.setattr(continuation_clock, "finish_round", lose_response)
    monkeypatch.setattr("goal_brain_runner.run", no_native)

    _ = goal_controller.run_once(run.scope.project, run.scope.goal_id, panel, "new-service")

    assert view(run).active_intent_id == ""
    assert not view(run).hold
    assert view(run).last_result["status"] == "completed"
    with stage_store.connection(run.scope.project) as database:
        row = text_cursor(
            database, "SELECT status FROM goal_graph_steps WHERE step='settle'"
        ).fetchone()
        assert row is not None
        assert row[0] == "done"


def test_other_owner_cannot_commit_a_settlement_receipt(run: GoalRun) -> None:
    outcome = pending_settlement(run)
    result: Record = {
        "status": "completed",
        "summary": outcome.summary,
        "retryable": True,
        "native_session_id": "saved-native-session",
        "receipt": run.scope.project + "/saved-actions.json",
        "dispatched_task_ids": list(outcome.dispatched_task_ids),
    }
    forged = run.scope.model_copy(update={"owner_id": "foreign-owner"})
    request = goal_settlement.prepare(forged, result)
    before = view(run)

    with pytest.raises(ValueError, match="settlement_scope_or_result_conflict"):
        _ = continuation_clock.finish_round(
            run.scope.project,
            run.scope.goal_id,
            run.scope.operation_id,
            run.scope.epoch,
            result,
            settlement=request,
        )

    assert view(run) == before


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE goal_graph_steps SET version=1 WHERE step='settle'",
        (
            "UPDATE goal_graph_steps SET result_json="
            "json_set(result_json,'$.settle_receipts_version',0) WHERE step='snapshot'"
        ),
        "UPDATE goal_graph_steps SET status='inflight' WHERE step='actions'",
    ],
)
def test_old_or_incomplete_settlement_cannot_resume(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    _ = pending_settlement(run)
    with stage_store.connection(run.scope.project) as database:
        _ = database.execute(change)
    before = view(run)
    monkeypatch.setattr("goal_brain_runner.run", no_native)

    result = goal_controller.run_once(run.scope.project, run.scope.goal_id, panel, "new-service")

    assert result is None
    assert view(run) == before


def test_historical_settlement_replay_preserves_later_stop(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = pending_settlement(run)
    monkeypatch.setattr("goal_brain_runner.run", no_native)
    _ = goal_controller.run_once(run.scope.project, run.scope.goal_id, panel, "new-service")
    result = TypeAdapter(Record).validate_python(view(run).last_result)
    request = goal_settlement.prepare(run.scope, result)
    _ = continuation_clock.control(run.scope.project, run.scope.goal_id, "stop")
    before = view(run)

    _ = continuation_clock.finish_round(
        run.scope.project,
        run.scope.goal_id,
        run.scope.operation_id,
        run.scope.epoch,
        result,
        settlement=request,
    )

    assert view(run) == before


def test_changed_late_settlement_cannot_replace_the_original_result(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = pending_settlement(run)
    monkeypatch.setattr("goal_brain_runner.run", no_native)
    _ = goal_controller.run_once(run.scope.project, run.scope.goal_id, panel, "new-service")
    result = TypeAdapter(Record).validate_python(
        {**view(run).last_result, "summary": "A different late result"}
    )
    request = goal_settlement.prepare(run.scope, result)
    before = view(run)

    with pytest.raises(ValueError, match="settlement_scope_or_result_conflict"):
        _ = continuation_clock.finish_round(
            run.scope.project,
            run.scope.goal_id,
            run.scope.operation_id,
            run.scope.epoch,
            result,
            settlement=request,
        )

    assert view(run) == before
