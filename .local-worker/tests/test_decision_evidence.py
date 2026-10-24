"""Related saved task evidence must change review subjects before stage synchronization."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal, assert_never

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import stage_store
from plan_contracts import PlanError
from test_decision_scope import answer, question
from test_decision_scope import branch as branch
from test_decision_scope import goal as goal

if TYPE_CHECKING:
    from test_decision_scope import Branch


def attach(branch: Branch, key: str = "target") -> Path:
    stage = next(spec.id for spec in branch.specs if spec.key == key)
    task_id = "fixture_" + key
    run = branch.project.project / ".model-desk/runs" / task_id
    run.mkdir(parents=True)
    _ = (run / "task.json").write_text(
        json.dumps(
            {
                "id": task_id,
                "project": str(branch.project.project),
                "goal_id": branch.project.goal_id,
                "stage_id": stage,
                "status": "completed",
                "engine": "codex_cli",
                "answer": "Original result",
            }
        )
    )
    with stage_store.connection(str(branch.project.project)) as database:
        _ = database.execute(
            "UPDATE stages SET task_ids=? WHERE id=?", (json.dumps([task_id]), stage)
        )
    return run


def refresh(branch: Branch) -> Branch:
    return replace(branch, card=question(branch.project, branch.stage_id))


@pytest.mark.parametrize("key", ["base", "target"])
def test_saved_task_result_changes_before_stage_sync_expire_choice(
    branch: Branch, key: str
) -> None:
    run = attach(branch, key)
    original = refresh(branch)
    path = run / "task.json"
    _ = path.write_text(path.read_text().replace("Original result", "Changed result"))
    with pytest.raises(PlanError, match="已变化"):
        _ = answer(original)


@pytest.mark.parametrize("name", ["review.json", "handover.json"])
def test_related_acceptance_or_handover_changes_expire_choice(branch: Branch, name: str) -> None:
    run = attach(branch)
    path = run / name
    _ = path.write_text(
        json.dumps(
            {
                "schema": 1,
                "task_id": run.name,
                "stage_id": branch.stage_id,
                "goal_id": branch.project.goal_id,
                "project": str(branch.project.project),
                "summary": "Original result",
            }
        )
    )
    original = refresh(branch)
    _ = path.write_text(path.read_text().replace("Original result", "Changed result"))
    with pytest.raises(PlanError, match="已变化"):
        _ = answer(original)


def test_new_acceptance_evidence_after_question_expires_choice(branch: Branch) -> None:
    run = attach(branch)
    original = refresh(branch)
    _ = (run / "review.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "task_id": run.name,
                "stage_id": branch.stage_id,
                "goal_id": branch.project.goal_id,
                "state": "executor_completed",
            }
        )
    )
    with pytest.raises(PlanError, match="已变化"):
        _ = answer(original)


def test_unrelated_task_result_does_not_expire_choice(branch: Branch) -> None:
    run = attach(branch, "other")
    original = refresh(branch)
    path = run / "task.json"
    _ = path.write_text(path.read_text().replace("Original result", "Changed result"))
    assert answer(original).status == "answered"


@pytest.mark.parametrize("defect", ["missing", "foreign", "oversize"])
def test_incomplete_associated_task_cannot_create_a_review(
    branch: Branch, defect: Literal["missing", "foreign", "oversize"]
) -> None:
    run = attach(branch)
    path = run / "task.json"
    match defect:
        case "missing":
            path.unlink()
        case "foreign":
            _ = path.write_text(path.read_text().replace(branch.project.goal_id, "foreign_goal"))
        case "oversize":
            _ = path.write_text('{"padding":"' + "x" * 262144 + '"}')
        case _:
            assert_never(defect)
    with pytest.raises(PlanError, match="证据"):
        _ = question(branch.project, branch.stage_id)
