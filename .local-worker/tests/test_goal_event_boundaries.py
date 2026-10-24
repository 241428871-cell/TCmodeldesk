"""Native callbacks, source transactions and snapshot delivery retain exact event identity."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol

import pytest

from model_desk_control import durable_leases
from model_desk_control.database import text_cursor
from model_desk_control.host_events import EventError
from model_desk_control.host_queue_models import Process as ProcessIdentity

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import continuation_clock
import goal_events
import human_decisions
import lease_descendants
import lease_runtime
import lease_storage
import model_registry as registry
import panel_server as panel
import stage_store
from plan_contracts import DecisionAnswer
from test_decision_scope import Card
from test_goal_action_receipts import GoalRun, apply, proposal
from test_goal_action_receipts import run as run
from test_goal_event_recovery import claim, inbox
from test_goal_events import claim_event, completed_task, finish_initial
from test_goal_resume import view
from test_task_queue import capture

if TYPE_CHECKING:
    from goal_brain_contract import Record
    from pydantic import JsonValue


class PanelControls(Protocol):
    def update_review(self, task_id: str, body: Record, *, event_intent_id: str = "") -> Record: ...


def controls() -> PanelControls:
    return panel


CONTROLS: Final = controls()


def test_worker_exit_records_the_event_without_a_manual_sync(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    callbacks = capture(monkeypatch)
    task_id = apply(run, proposal(run)).dispatched_task_ids[0]
    finish_initial(run)

    class Process:
        pid: int = 900002

        def wait(self) -> int:
            _ = registry.patch_task(registry.task_dir(task_id), status="completed")
            saved = registry.read_task(registry.task_dir(task_id))
            receipt = lease_runtime.current(str(saved["execution_lease_token"]))
            lease_descendants.sample(registry.HERE, receipt, final=True)
            return 0

    def launch(*_args: JsonValue, **_kwargs: JsonValue) -> Process:
        # This mock replaces lease_runner too; simulate its registration before completion.
        saved = registry.read_task(registry.task_dir(task_id))
        with lease_storage.transaction() as database:
            receipt = durable_leases.read(database, str(saved["execution_lease_token"]))
            assert receipt is not None
            _ = durable_leases.started(
                database,
                receipt,
                ProcessIdentity(pid=Process.pid, birth=1.0),
                containment="posix_session",
            )
            lease_descendants.initialize(
                database, receipt.token, ProcessIdentity(pid=Process.pid, birth=1.0)
            )
        return Process()

    monkeypatch.setattr(subprocess, "Popen", launch)
    callbacks[0]()
    assert claim_event(run)["trigger_kind"] == "event"


def test_round_caused_review_change_does_not_wake_itself_again(run: GoalRun) -> None:
    task_id = completed_task(run)
    event_claim = claim_event(run)
    state = registry.read_task(registry.task_dir(task_id))
    # Use the original shared review endpoint, with independent main-brain evidence.
    _ = CONTROLS.update_review(
        task_id,
        {"action": "block", "reason": "An independent review found missing evidence"},
        event_intent_id=str(event_claim["id"]),
    )
    assert inbox(run) == ()
    with stage_store.connection(run.scope.project) as database:
        rows = text_cursor(database, "SELECT claimed_by FROM goal_events ORDER BY seq").fetchall()
    assert len(rows) == 2
    assert all(row[0] == event_claim["id"] for row in rows)
    assert registry.read_task(registry.task_dir(task_id))["id"] == state["id"]


def test_periodic_due_coalesces_pending_event_into_the_same_round(run: GoalRun) -> None:
    _ = completed_task(run)
    original = inbox(run)
    _ = continuation_clock.control(run.scope.project, run.scope.goal_id, "run_now")
    current = claim_event(run)
    assert current["trigger_kind"] == "clock"
    assert current["event_ids"] == list(original)
    assert inbox(run) == ()


def test_fresh_scan_is_read_only_and_cannot_manufacture_an_event(run: GoalRun) -> None:
    finish_initial(run)
    assert not goal_events.ready(run.scope.project, run.scope.goal_id, run.scope.epoch)
    with stage_store.connection(run.scope.project) as database:
        rows = text_cursor(
            database, "SELECT name FROM sqlite_master WHERE name='goal_events'"
        ).fetchall()
    assert rows == []
    assert claim(run) is None


def test_claimed_event_context_rejects_another_round(run: GoalRun) -> None:
    _ = completed_task(run)
    current = claim_event(run)
    with pytest.raises(EventError, match="event_context_receipt_missing"):
        _ = goal_events.context(
            run.scope.project, run.scope.goal_id, {**current, "id": "foreign_round"}
        )


def test_lost_event_write_rolls_back_the_actual_human_answer(run: GoalRun) -> None:
    _ = apply(run, proposal(run, dispatch=False, question=True))
    finish_initial(run)
    card = Card.model_validate_json(
        json.dumps(human_decisions.list_decisions(run.scope.project, run.scope.goal_id)[0])
    )
    with stage_store.connection(run.scope.project) as database:
        # DDL collision injects failure at event insertion without patching the answer itself.
        _ = database.execute("CREATE TABLE goal_events(wrong_column TEXT)")
    with pytest.raises(sqlite3.OperationalError):
        _ = human_decisions.answer_decision(
            run.scope.project,
            card.id,
            DecisionAnswer(
                subject_hash=card.subject_hash,
                authority_epoch=card.fingerprint.authority_epoch,
                option_id=card.options[0].id,
            ),
        )
    after = Card.model_validate_json(
        json.dumps(human_decisions.list_decisions(run.scope.project, run.scope.goal_id)[0])
    )
    assert after.status == "pending"


def test_failed_round_retry_backoff_cannot_be_skipped_by_another_event(run: GoalRun) -> None:
    task_id = completed_task(run)
    current = claim_event(run)
    _ = continuation_clock.finish_round(
        run.scope.project,
        run.scope.goal_id,
        str(current["id"]),
        run.scope.epoch,
        {"status": "failed", "summary": "Retry later", "retryable": True},
    )
    _ = CONTROLS.update_review(
        task_id,
        {"action": "block", "reason": "Independent review arrived during backoff"},
    )
    before = view(run)
    assert claim(run) is None
    assert not goal_events.ready(run.scope.project, run.scope.goal_id, run.scope.epoch)
    assert view(run) == before


def test_new_authorized_round_can_observe_review_of_an_older_task(run: GoalRun) -> None:
    task_id = completed_task(run)
    for action in ("stop", "resume", "run_now"):
        _ = continuation_clock.control(run.scope.project, run.scope.goal_id, action)
    current = claim_event(run)
    _ = CONTROLS.update_review(
        task_id,
        {"action": "block", "reason": "Current main-brain review of original output"},
        event_intent_id=str(current["id"]),
    )
    with stage_store.connection(run.scope.project) as database:
        rows = text_cursor(
            database, "SELECT claimed_by,CAST(epoch AS TEXT) FROM goal_events ORDER BY seq"
        ).fetchall()
        stage = text_cursor(
            database, "SELECT status FROM stages WHERE goal_id=?", (run.scope.goal_id,)
        ).fetchone()
    assert stage is not None
    assert stage[0] == "blocked"
    assert rows[-1][0] == current["id"]
    assert rows[-1][1] == str(current["authority_epoch"])
