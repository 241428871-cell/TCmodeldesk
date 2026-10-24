"""Saved action batches use real temporary project records and queue entrypoints."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Protocol

import pytest
from pydantic import BaseModel, ConfigDict

from model_desk_control.database import text_cursor
from model_desk_control.host_flow import HostApplied, HostRef

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import brain_requests
import continuation_clock
import dynamic_plan
import execution_endpoints
import goal_actions
import goal_effects
import human_decisions
import model_registry as registry
import panel_server as panel
import stage_store
import test_goal_controller as fixtures
import workspace_config

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from sqlite3 import Connection

    from goal_brain_contract import Record

    from model_desk_control.host_effects import EffectRequest


class LegacyFixture(Protocol):
    project: Path
    project_text: str
    goal: str
    model_id: str

    def setUp(self) -> None: ...
    def doCleanups(self) -> None: ...


@dataclass(frozen=True, slots=True)
class GoalRun:
    scope: HostRef
    model_id: str
    queued: list[str]


class PlanView(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="ignore")
    plan_revision: int


class Option(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="ignore")
    id: str


class DecisionView(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="ignore")
    id: str
    status: str
    subject_hash: str
    options: tuple[Option, ...]


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch) -> Generator[GoalRun]:
    case: LegacyFixture = fixtures.GoalControllerTests()
    case.setUp()
    queued: list[str] = []

    def submit(_function: Callable[[str], None], task_id: str) -> None:
        queued.append(task_id)

    try:
        _ = execution_endpoints.update(
            "codex_cli", {"enabled": True, "current_model": case.model_id}, {}
        )
        _ = workspace_config.update_binding(
            {
                "project": case.project_text,
                "model_id": case.model_id,
                "role": "execution",
                "is_default": True,
            }
        )
        monkeypatch.setattr(panel.EXECUTOR, "submit", submit)
        claim = continuation_clock.claim_due(case.project_text, case.goal, "fixture-action-owner")
        assert claim is not None
        yield GoalRun(
            HostRef(
                project=case.project_text,
                goal_id=case.goal,
                operation_id=claim["id"],
                epoch=claim["authority_epoch"],
                owner_id=claim["owner_id"],
            ),
            case.model_id,
            queued,
        )
    finally:
        case.doCleanups()


def proposal(
    run: GoalRun, *, dispatch: bool = True, question: bool = False, edit: bool = False
) -> Record:
    stage: Record = {
        "key": "build",
        "title": "Inspect inputs",
        "objective": "Read current project inputs",
        "kind": "edit" if edit else "chat",
        "depends_on": [],
        "files": ["generated.txt"] if edit else [],
        "acceptance": ["Inputs are identified"],
        "candidates": [run.model_id],
        "fixed_model": run.model_id,
    }
    return {
        "plan": {"base_revision": 0, "reason": "Inspect before implementation", "stages": [stage]},
        "questions": [
            {
                "question": "Choose the output format",
                "reason": "The fixed criteria allow two formats",
                "options": ["Use text", "Use a table"],
                "approval_option": "Use text",
                "recommendation": "Use text",
                "evidence_refs": [],
                "affected_stage_ids": ["build"],
            }
        ]
        if question
        else [],
        "dispatch_stage_ids": ["build"] if dispatch else [],
        "reviews": [],
        "obligations": [],
        "complete": False,
        "summary": "Inspect current inputs",
    }


def apply(run: GoalRun, actions: Record) -> HostApplied:
    result = goal_actions.apply(
        run.scope.project,
        run.scope.goal_id,
        run.scope.epoch,
        actions,
        panel,
        {},
        str(Path(run.scope.project) / "saved-actions.json"),
        scope=run.scope,
    )
    return HostApplied.model_validate_json(json.dumps(result))


def plan_view(run: GoalRun) -> PlanView:
    return PlanView.model_validate_json(
        json.dumps(
            dynamic_plan.describe_goal(run.scope.project, run.scope.goal_id),
        )
    )


def test_saved_batch_reuses_original_plan_revision_and_attributed_task(run: GoalRun) -> None:
    actions = proposal(run)
    first = apply(run, actions)
    repeated = apply(run, actions)
    assert repeated.dispatched_task_ids == first.dispatched_task_ids == tuple(run.queued)
    assert len(run.queued) == 1
    assert plan_view(run).plan_revision == 1


def test_atomic_plan_and_receipt_rollback_together_if_commit_fails(
    run: GoalRun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = goal_effects.save

    def lose_commit(
        database: Connection,
        request: EffectRequest | None,
        subject_id: str,
        revision: int | None = None,
    ) -> None:
        original(database, request, subject_id, revision)
        if revision is not None:
            reason = "Fixture failed after writing plan and receipt, before commit"
            raise OSError(reason)

    monkeypatch.setattr(goal_effects, "save", lose_commit)
    with pytest.raises(OSError, match="Fixture failed after writing"):
        _ = apply(run, proposal(run, dispatch=False))
    assert plan_view(run).plan_revision == 0
    with stage_store.connection(run.scope.project) as database:
        rows = text_cursor(
            database, "SELECT request_id FROM brain_requests WHERE kind='goal_plan'"
        ).fetchall()
    assert rows == []


def test_replaying_an_answered_question_does_not_create_a_new_human_request(run: GoalRun) -> None:
    actions = proposal(run, dispatch=False, question=True)
    _ = apply(run, actions)
    before = DecisionView.model_validate_json(
        json.dumps(
            human_decisions.list_decisions(run.scope.project, run.scope.goal_id)[0],
        )
    )
    _ = human_decisions.answer_decision(
        run.scope.project,
        before.id,
        {
            "subject_hash": before.subject_hash,
            "authority_epoch": run.scope.epoch,
            "option_id": before.options[0].id,
        },
    )
    _ = apply(run, actions)
    decisions = human_decisions.list_decisions(run.scope.project, run.scope.goal_id)
    assert len(decisions) == 1
    after = DecisionView.model_validate_json(json.dumps(decisions[0]))
    assert after.id == before.id
    assert after.status == "answered"


@pytest.mark.parametrize("status", ["unknown", "reserved"])
def test_saved_unknown_dispatch_recovers_original_task_without_queuing_again(
    run: GoalRun,
    status: str,
) -> None:
    actions = proposal(run)
    first = apply(run, actions)
    with stage_store.connection(run.scope.project) as database:
        _ = database.execute(
            "UPDATE brain_requests SET state=?,receipt='{}' WHERE kind='dispatch'", (status,)
        )
    recovered = apply(run, actions)
    assert recovered.dispatched_task_ids == first.dispatched_task_ids
    assert len(run.queued) == 1


@pytest.mark.parametrize("field", ["goal_round_id", "goal_action_payload_digest"])
def test_unknown_dispatch_without_matching_original_task_stays_unknown(
    run: GoalRun,
    field: str,
) -> None:
    actions = proposal(run)
    first = apply(run, actions)
    _ = registry.patch_task(registry.task_dir(first.dispatched_task_ids[0]), **{field: "foreign"})
    with stage_store.connection(run.scope.project) as database:
        _ = database.execute(
            "UPDATE brain_requests SET state='unknown',receipt='{}' WHERE kind='dispatch'"
        )
    with pytest.raises(brain_requests.UnknownOutcome):
        _ = apply(run, actions)
    assert len(run.queued) == 1


def test_original_round_cannot_receive_different_actions_during_recovery(run: GoalRun) -> None:
    _ = apply(run, proposal(run, dispatch=False))
    changed = proposal(run)
    with pytest.raises(ValueError, match="effect_payload_conflict"):
        _ = apply(run, changed)
    assert run.queued == []
