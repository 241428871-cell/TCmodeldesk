"""Production stage branches park on real human cards and resume through original receipts."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

import pytest
from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter

from model_desk_control.database import text_cursor
from model_desk_control.host_flow import HostApplied, HostRef

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import continuation_clock
import dynamic_plan
import goal_actions
import goal_brain_runner
import goal_controller
import goal_scan
import human_decisions
import panel_server as panel
import stage_store
import task_graph_bridge
from plan_contracts import DecisionAnswer
from test_decision_scope import Card
from test_goal_action_receipts import run as run

if TYPE_CHECKING:
    from goal_brain_contract import Record
    from test_goal_action_receipts import GoalRun


class Branch(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="ignore")
    stage_id: str
    status: str
    decision_id: str
    epoch: int


class Row(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="ignore")
    status: str
    task_id: str
    reason: str


def stage(run: GoalRun, key: str) -> Record:
    return {
        "key": key,
        "title": "Inspect " + key,
        "objective": "Read the inputs for " + key,
        "kind": "chat",
        "depends_on": [],
        "files": [],
        "acceptance": ["Inputs for " + key + " are identified"],
        "candidates": [run.model_id],
        "fixed_model": run.model_id,
    }


def branching(run: GoalRun) -> Record:
    """One branch needs a human exception; its sibling has no dependency on that answer."""
    return {
        "plan": {
            "base_revision": 0,
            "reason": "Two independent inspections",
            "stages": [stage(run, "build"), stage(run, "docs")],
        },
        "questions": [
            {
                "question": "May the build branch read the restricted fixture?",
                "reason": "The fixed criteria require approval for restricted inputs",
                "options": ["Allow", "Do not allow"],
                "approval_option": "Allow",
                "recommendation": "Allow",
                "evidence_refs": [],
                "affected_stage_ids": ["build"],
            }
        ],
        "dispatch_stage_ids": ["build", "docs"],
        "reviews": [],
        "obligations": [],
        "complete": False,
        "summary": "Inspect both branches",
    }


def idle() -> Record:
    return {
        "plan": None,
        "questions": [],
        "dispatch_stage_ids": [],
        "reviews": [],
        "obligations": [],
        "complete": False,
        "summary": "Observe the current branches",
    }


def apply_in(scope: HostRef, actions: Record) -> HostApplied:
    result = goal_actions.apply(
        scope.project,
        scope.goal_id,
        scope.epoch,
        actions,
        panel,
        {},
        str(Path(scope.project) / "saved-actions.json"),
        scope=scope,
    )
    return HostApplied.model_validate_json(json.dumps(result))


def branches(run: GoalRun) -> list[Branch]:
    rows = task_graph_bridge.listing(run.scope.project, run.scope.goal_id)
    return [Branch.model_validate_json(json.dumps(row)) for row in rows]


def stored(run: GoalRun) -> list[Row]:
    with stage_store.connection(run.scope.project) as database:
        rows = text_cursor(
            database, "SELECT status,task_id,reason FROM goal_task_branches ORDER BY created_at"
        ).fetchall()
    return [Row(status=row[0], task_id=row[1], reason=row[2]) for row in rows]


def stage_id(run: GoalRun, key: str) -> str:
    plan = dynamic_plan.describe_goal(run.scope.project, run.scope.goal_id)
    stages = TypeAdapter(list[dict[str, JsonValue]]).validate_python(plan["stages"])
    return next(str(row["id"]) for row in stages if row.get("key") == key)


def card(run: GoalRun) -> Card:
    rows = human_decisions.list_decisions(run.scope.project, run.scope.goal_id)
    return Card.model_validate_json(json.dumps(rows[0]))


def finish(scope: HostRef) -> None:
    _ = continuation_clock.finish_round(
        scope.project,
        scope.goal_id,
        scope.operation_id,
        scope.epoch,
        {"status": "completed", "summary": "Round settled"},
    )


def answer(run: GoalRun, option: int) -> None:
    current = card(run)
    _ = human_decisions.answer_decision(
        run.scope.project,
        current.id,
        DecisionAnswer(
            subject_hash=current.subject_hash,
            authority_epoch=current.fingerprint.authority_epoch,
            option_id=current.options[option].id,
        ),
    )


def next_round(run: GoalRun, owner: str) -> HostRef:
    claim = continuation_clock.claim_due(
        run.scope.project, run.scope.goal_id, owner, host_protocol="graph_v2"
    )
    assert claim is not None, "A saved answer is an event for the original clock"
    return HostRef(
        project=run.scope.project,
        goal_id=run.scope.goal_id,
        operation_id=claim["id"],
        epoch=claim["authority_epoch"],
        owner_id=claim["owner_id"],
    )


def parked(run: GoalRun) -> HostApplied:
    first = apply_in(run.scope, branching(run))
    finish(run.scope)
    return first


def test_pending_exception_parks_only_its_own_branch(run: GoalRun) -> None:
    first = parked(run)
    assert first.dispatched_task_ids == tuple(run.queued)
    assert len(run.queued) == 1
    docs = stage_store.get_stage(stage_id(run, "docs"), run.scope.project)
    assert docs is not None
    assert docs["active_task_id"] == run.queued[0]
    waiting = branches(run)
    assert [(row.stage_id, row.status) for row in waiting] == [(stage_id(run, "build"), "waiting")]
    assert waiting[0].decision_id == card(run).id
    assert task_graph_bridge.checkpoint_path(run.scope.project).is_file()


def test_saved_answer_resumes_that_branch_once_in_the_next_round(run: GoalRun) -> None:
    _ = parked(run)
    answer(run, 0)
    second = next_round(run, "answer-driver")
    resumed = apply_in(second, idle())
    assert len(run.queued) == 2
    assert resumed.dispatched_task_ids == (run.queued[1],)
    build = stage_store.get_stage(stage_id(run, "build"), run.scope.project)
    assert build is not None
    assert build["active_task_id"] == run.queued[1]
    replayed = apply_in(second, idle())
    assert replayed.dispatched_task_ids == ()
    assert len(run.queued) == 2
    assert branches(run) == []
    assert [row.status for row in stored(run)] == ["dispatched", "dispatched"]


def test_denied_exception_closes_the_branch_for_replanning(run: GoalRun) -> None:
    _ = parked(run)
    answer(run, 1)
    second = next_round(run, "answer-driver")
    result = apply_in(second, idle())
    assert result.dispatched_task_ids == ()
    assert len(run.queued) == 1
    rows = stored(run)
    assert [row.status for row in rows] == ["retired", "dispatched"]
    assert "重新规划" in rows[0].reason


def test_lost_checkpoint_reuses_domain_facts_without_repeating_dispatch(run: GoalRun) -> None:
    _ = parked(run)
    task_graph_bridge.checkpoint_path(run.scope.project).unlink()
    answer(run, 0)
    second = next_round(run, "answer-driver")
    _ = apply_in(second, idle())
    assert len(run.queued) == 2
    task_graph_bridge.checkpoint_path(run.scope.project).unlink()
    _ = apply_in(second, idle())
    finish(second)
    _ = continuation_clock.control(run.scope.project, run.scope.goal_id, "run_now")
    third = next_round(run, "brain-repeats-request")
    again = branching(run) | {"plan": None, "questions": []}
    assert apply_in(third, again).dispatched_task_ids == ()
    assert len(run.queued) == 2


def test_stop_retires_the_old_epoch_branch_without_dispatch(run: GoalRun) -> None:
    _ = parked(run)
    _ = continuation_clock.control(run.scope.project, run.scope.goal_id, "stop")
    _ = continuation_clock.control(run.scope.project, run.scope.goal_id, "start")
    _ = continuation_clock.control(run.scope.project, run.scope.goal_id, "run_now")
    later = next_round(run, "after-stop")
    assert later.epoch != run.scope.epoch
    result = apply_in(later, idle())
    assert result.dispatched_task_ids == ()
    assert len(run.queued) == 1
    assert [row.status for row in stored(run)] == ["retired", "dispatched"]


def test_observation_never_opens_a_graph_or_dispatches(run: GoalRun) -> None:
    _ = parked(run)
    answer(run, 0)
    path = task_graph_bridge.checkpoint_path(run.scope.project)
    before = path.stat().st_mtime_ns
    assert (
        goal_scan.decide(run.scope.project, run.scope.goal_id, "active", "observer")
        == goal_scan.ScanAction.RUN
    )
    _ = task_graph_bridge.listing(run.scope.project, run.scope.goal_id)
    _ = human_decisions.list_decisions(run.scope.project, run.scope.goal_id)
    _ = continuation_clock.snapshot(run.scope.project, run.scope.goal_id)
    _ = stage_store.sync_from_tasks(run.scope.goal_id, project=run.scope.project)
    assert path.stat().st_mtime_ns == before
    assert len(run.queued) == 1
    assert [row.status for row in branches(run)] == ["waiting"]


def test_old_round_cannot_resume_a_branch_after_its_clock_moved(run: GoalRun) -> None:
    _ = parked(run)
    answer(run, 0)
    with pytest.raises(ValueError, match=r"当前原生轮次|授权"):
        _ = apply_in(run.scope, idle())
    assert len(run.queued) == 1


def test_answer_event_round_resumes_the_branch_through_the_goal_graph(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = parked(run)
    answer(run, 0)
    contexts: list[Record] = []

    def native(context: Record, *_args: str, **_kwargs: str) -> Record:
        contexts.append(context)
        return {
            "actions": idle(),
            "native_session_id": "answer-session",
            "receipt": run.scope.project + "/answer-result.json",
        }

    monkeypatch.setattr(goal_brain_runner, "run", native)
    assert goal_controller.run_once(run.scope.project, run.scope.goal_id, panel) is not None
    assert len(contexts) == 1
    assert len(run.queued) == 2
    assert branches(run) == []
    assert goal_controller.run_once(run.scope.project, run.scope.goal_id, panel) is None
    assert len(run.queued) == 2
