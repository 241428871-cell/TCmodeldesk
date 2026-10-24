"""Real source events use the original clock and cannot manufacture another scheduler."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import JsonValue, TypeAdapter

from model_desk_control.host_event_context import EventTrigger

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import continuation_clock
import goal_brain_runner
import goal_controller
import goal_scan
import human_decisions
import model_registry as registry
import panel_server as panel
import stage_store
from plan_contracts import DecisionAnswer
from test_decision_scope import Card
from test_goal_action_receipts import GoalRun, apply, proposal
from test_goal_action_receipts import run as run
from test_goal_resume import view

if TYPE_CHECKING:
    from goal_brain_contract import Record


def finish_initial(run: GoalRun) -> None:
    _ = continuation_clock.finish_round(
        run.scope.project,
        run.scope.goal_id,
        run.scope.operation_id,
        run.scope.epoch,
        {"status": "completed", "summary": "Initial work dispatched"},
    )


def completed_task(run: GoalRun) -> str:
    result = apply(run, proposal(run))
    task_id = result.dispatched_task_ids[0]
    finish_initial(run)
    _ = registry.patch_task(registry.task_dir(task_id), status="completed", ended_at=registry.now())
    _ = stage_store.sync_from_tasks(run.scope.goal_id, project=run.scope.project)
    return task_id


def claim_event(run: GoalRun) -> Record:
    claim = continuation_clock.claim_due(
        run.scope.project, run.scope.goal_id, "new-driver", host_protocol="graph_v2"
    )
    assert claim is not None, "An actual completion must advance before the unchanged due time"
    return TypeAdapter(dict[str, JsonValue]).validate_json(json.dumps(claim))


def test_real_task_event_claim_preserves_cadence_and_is_single_use(run: GoalRun) -> None:
    _ = completed_task(run)
    before = view(run)
    assert (
        goal_scan.decide(run.scope.project, run.scope.goal_id, "active", "next")
        == goal_scan.ScanAction.RUN
    )
    claim = claim_event(run)
    assert claim["trigger_kind"] == "event"
    assert claim["event_ids"]
    assert view(run).next_due_at == before.next_due_at
    assert view(run).interval_seconds == before.interval_seconds
    _ = continuation_clock.finish_round(
        run.scope.project,
        run.scope.goal_id,
        str(claim["id"]),
        run.scope.epoch,
        {"status": "completed", "summary": "Event inspected"},
    )
    assert (
        continuation_clock.claim_due(
            run.scope.project, run.scope.goal_id, "again", host_protocol="graph_v2"
        )
        is None
    )


@pytest.mark.parametrize("option_index", [0, 1])
def test_answer_is_an_event_without_requiring_an_extra_continue_button(
    run: GoalRun, option_index: int
) -> None:
    _ = apply(run, proposal(run, dispatch=False, question=True))
    finish_initial(run)
    card = Card.model_validate_json(
        json.dumps(human_decisions.list_decisions(run.scope.project, run.scope.goal_id)[0])
    )
    _ = human_decisions.answer_decision(
        run.scope.project,
        card.id,
        DecisionAnswer(
            subject_hash=card.subject_hash,
            authority_epoch=card.fingerprint.authority_epoch,
            option_id=card.options[option_index].id,
        ),
    )
    assert claim_event(run)["trigger_kind"] == "event"


def test_event_enters_actual_langgraph_round_only_once(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = completed_task(run)
    before = view(run)
    contexts: list[Record] = []

    def native(context: Record, *_args: str, **_kwargs: str) -> Record:
        contexts.append(context)
        actions = proposal(run, dispatch=False)
        actions["plan"] = None
        return {
            "actions": actions,
            "native_session_id": "event-session",
            "receipt": run.scope.project + "/event-result.json",
        }

    monkeypatch.setattr(goal_brain_runner, "run", native)
    assert goal_controller.run_once(run.scope.project, run.scope.goal_id, panel) is not None
    assert len(contexts) == 1
    trigger = EventTrigger.model_validate_json(json.dumps(contexts[0]["trigger"]))
    assert trigger.kind == "event"
    assert trigger.events[0].kind == "task_observed"
    assert view(run).next_due_at == before.next_due_at
    assert goal_controller.run_once(run.scope.project, run.scope.goal_id, panel) is None
    assert len(contexts) == 1


@pytest.mark.parametrize("control", ["pause", "stop", "hold"])
def test_event_cannot_override_current_human_intent(run: GoalRun, control: str) -> None:
    _ = completed_task(run)
    _ = continuation_clock.control(
        run.scope.project,
        run.scope.goal_id,
        control,
        {"kind": "unknown", "reason": "Original effects need reconciliation"}
        if control == "hold"
        else {},
    )
    before = view(run)
    assert (
        continuation_clock.claim_due(
            run.scope.project, run.scope.goal_id, "new", host_protocol="graph_v2"
        )
        is None
    )
    assert view(run) == before
