"""Exercise new and historical file authority through the actual legacy goal store."""

import json
import sys
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Literal, Protocol

import pytest
from pydantic import BaseModel, ConfigDict

from model_desk_control.database import text_cursor

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import decision_records
import dynamic_plan
import governance_context
import stage_store
import test_goal_controller as fixtures
from plan_contracts import GoalContract, PlanError, PlanPatch, StageSpec


class LegacyFixture(Protocol):
    """Typed seam to the existing temporary project/provider fixture."""

    project: Path
    project_text: str
    goal: str

    def setUp(self) -> None: ...
    def doCleanups(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ProjectGoal:
    project: Path
    goal_id: str


class StageView(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="ignore")
    id: str
    files: tuple[str, ...]


class PlanView(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="ignore")
    file_scope: Literal["selected", "project"]
    review_policy: Literal["exceptions", "milestones", "final"]
    allowed_files: tuple[str, ...]
    contract_revision: int
    plan_revision: int
    stages: tuple[StageView, ...]


@pytest.fixture
def goal() -> Generator[ProjectGoal]:
    case: LegacyFixture = fixtures.GoalControllerTests()
    case.setUp()
    try:
        yield ProjectGoal(case.project, case.goal)
    finally:
        _ = case.doCleanups()


def view(goal: ProjectGoal) -> PlanView:
    return PlanView.model_validate_json(
        json.dumps(dynamic_plan.describe_goal(str(goal.project), goal.goal_id))
    )


def revise(goal: ProjectGoal, name: str) -> PlanView:
    current = view(goal)
    patch = PlanPatch(
        base_revision=current.plan_revision,
        reason="Create the requested new artifact",
        stages=[
            StageSpec(
                key="implement",
                title="Create output",
                objective="Implement the requested behavior",
                kind="edit",
                files=[name],
                acceptance=["Actual artifact meets the criterion"],
            ),
        ],
    )
    return PlanView.model_validate_json(
        json.dumps(dynamic_plan.revise_plan(str(goal.project), goal.goal_id, patch))
    )


def test_new_goal_can_plan_a_new_file_without_human_enumeration(goal: ProjectGoal) -> None:
    initial = view(goal)
    assert initial.file_scope == "project"
    assert initial.allowed_files == ()
    result = revise(goal, "src/new.py")
    assert result.stages[0].files == ("src/new.py",)
    assert not (goal.project / "src/new.py").exists()


def test_old_contract_without_scope_is_not_widened_or_rewritten_on_read(goal: ProjectGoal) -> None:
    contract = (
        '{"criteria":["Keep the fixed criterion"],'
        '"review_policy":"exceptions","allowed_files":["a.py"]}'
    )
    with stage_store.connection(str(goal.project)) as database:
        _ = database.execute(
            "UPDATE goal_contracts SET payload=? WHERE goal_id=?", (contract, goal.goal_id)
        )
    assert view(goal).file_scope == "selected"
    with pytest.raises(PlanError, match="拒绝"):
        _ = revise(goal, "other.py")
    with stage_store.connection(str(goal.project)) as database:
        actual = text_cursor(
            database, "SELECT payload FROM goal_contracts WHERE goal_id=?", (goal.goal_id,)
        ).fetchone()
    assert actual is not None
    assert actual[0] == contract


def test_explicit_empty_selection_remains_read_only(goal: ProjectGoal) -> None:
    _ = dynamic_plan.configure_goal(
        str(goal.project),
        goal.goal_id,
        GoalContract(criteria=["Analyze without edits"], allowed_files=[]),
    )
    assert view(goal).file_scope == "selected"
    with pytest.raises(PlanError):
        _ = revise(goal, "new.py")


def test_criteria_update_preserves_explicit_authority_and_review_policy(goal: ProjectGoal) -> None:
    _ = dynamic_plan.configure_goal(
        str(goal.project),
        goal.goal_id,
        GoalContract(
            criteria=["Initial criterion"],
            file_scope="selected",
            allowed_files=["approved.py"],
            review_policy="final",
        ),
    )
    _ = dynamic_plan.configure_goal(
        str(goal.project), goal.goal_id, GoalContract(criteria=["Refined criterion"])
    )
    actual = view(goal)
    assert actual.file_scope == "selected"
    assert actual.allowed_files == ("approved.py",)
    assert actual.review_policy == "final"


def test_narrowing_authority_blocks_a_planned_edit_at_actual_dispatch_guard(
    goal: ProjectGoal,
) -> None:
    result = revise(goal, "new.py")
    _ = dynamic_plan.configure_goal(
        str(goal.project),
        goal.goal_id,
        GoalContract(
            criteria=["Keep behavior"], file_scope="selected", allowed_files=["allowed.py"]
        ),
    )
    with pytest.raises(governance_context.DispatchBlocked, match="收窄"):
        _ = governance_context.guard(
            str(goal.project), goal.goal_id, result.stages[0].id, files=["new.py"], kind="edit"
        )


def test_project_scope_final_artifacts_bind_to_ai_declared_files(goal: ProjectGoal) -> None:
    _ = revise(goal, "new.py")
    with stage_store.connection(str(goal.project)) as database:
        before = decision_records.artifact_digest(database, str(goal.project), goal.goal_id)
    _ = (goal.project / "new.py").write_text("Actual delivery bytes")
    with stage_store.connection(str(goal.project)) as database:
        after = decision_records.artifact_digest(database, str(goal.project), goal.goal_id)
    assert before != after


@pytest.mark.skipif(sys.platform == "win32", reason="Windows symlinks require developer privileges")
def test_dispatch_rechecks_alias_changed_after_planning(goal: ProjectGoal) -> None:
    safe = goal.project / "source"
    safe.mkdir()
    alias = goal.project / "alias"
    alias.symlink_to(safe, target_is_directory=True)
    plan = revise(goal, "alias/new.py")
    alias.unlink()
    alias.symlink_to(goal.project / ".model-desk", target_is_directory=True)
    with pytest.raises(governance_context.DispatchBlocked):
        _ = governance_context.guard(
            str(goal.project),
            goal.goal_id,
            plan.stages[0].id,
            files=["alias/new.py"],
            kind="edit",
        )


@pytest.mark.parametrize(
    "name", [".model-desk/task.json", ".local-worker/models.json", "../outside.py"]
)
def test_invalid_project_plan_leaves_current_revision_and_stages_unchanged(
    goal: ProjectGoal, name: str
) -> None:
    before = view(goal)
    with pytest.raises(PlanError):
        _ = revise(goal, name)
    assert view(goal) == before
