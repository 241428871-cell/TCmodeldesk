"""Current human questions belong to the affected branch and its real upstream evidence."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

import pytest
from pydantic import BaseModel, ConfigDict

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dynamic_plan
import human_decisions
import stage_store
from plan_contracts import DecisionAnswer, DecisionRequest, PlanError, PlanPatch, StageSpec
from test_goal_file_scope import goal as goal

if TYPE_CHECKING:
    from test_goal_file_scope import ProjectGoal


class Fingerprint(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="ignore")
    authority_epoch: int


class Option(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="ignore")
    id: str


class Card(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="ignore")
    id: str
    status: str
    subject_hash: str
    fingerprint: Fingerprint
    options: tuple[Option, ...]


class Stage(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="ignore")
    id: str
    key: str


class Plan(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="ignore")
    stages: tuple[Stage, ...]


@dataclass(frozen=True, slots=True)
class Branch:
    project: ProjectGoal
    stage_id: str
    specs: tuple[StageSpec, ...]
    card: Card


def question(project: ProjectGoal, stage_id: str) -> Card:
    result = human_decisions.create_decision(
        str(project.project),
        project.goal_id,
        DecisionRequest(
            question="Choose how to handle this branch",
            reason="The branch requires an explicit human choice",
            options=["Continue this proposal", "Revise the branch"],
            approval_option="Continue this proposal",
            affected_stage_ids=[stage_id],
        ),
    )
    return Card.model_validate_json(json.dumps(result))


@pytest.fixture
def branch(goal: ProjectGoal) -> Branch:
    specs = tuple(
        StageSpec(
            key=key,
            title=key,
            objective="Produce independently verifiable branch output",
            kind="edit",
            files=[key + ".txt"],
            depends_on=["base"] if key == "target" else [],
            acceptance=["Output satisfies the fixed criterion"],
        )
        for key in ("base", "target", "other")
    )
    for spec in specs:
        _ = (goal.project / spec.files[0]).write_text(spec.key)
    result = Plan.model_validate_json(
        json.dumps(
            dynamic_plan.revise_plan(
                str(goal.project),
                goal.goal_id,
                PlanPatch(base_revision=0, reason="Split independent branches", stages=list(specs)),
            )
        )
    )
    identities = {stage.key: stage.id for stage in result.stages}
    stored = tuple(spec.model_copy(update={"id": identities[spec.key]}) for spec in specs)
    return Branch(goal, identities["target"], stored, question(goal, identities["target"]))


def answer(branch: Branch) -> Card:
    result = human_decisions.answer_decision(
        str(branch.project.project),
        branch.card.id,
        DecisionAnswer(
            subject_hash=branch.card.subject_hash,
            authority_epoch=branch.card.fingerprint.authority_epoch,
            option_id=branch.card.options[0].id,
        ),
    )
    return Card.model_validate_json(json.dumps(result))


def change_plan(branch: Branch, key: str) -> None:
    specs = [
        spec.model_copy(update={"objective": "Revised from actual evidence"})
        if spec.key == key
        else spec
        for spec in branch.specs
    ]
    _ = dynamic_plan.revise_plan(
        str(branch.project.project),
        branch.project.goal_id,
        PlanPatch(base_revision=1, reason="Revise only one branch", stages=specs),
    )


def test_unrelated_file_does_not_expire_pending_branch_choice(branch: Branch) -> None:
    _ = (branch.project.project / "other.txt").write_text("New unrelated output")
    assert answer(branch).status == "answered"


def test_unrelated_plan_revision_does_not_expire_pending_branch_choice(branch: Branch) -> None:
    change_plan(branch, "other")
    assert answer(branch).status == "answered"


def test_same_question_deduplicates_after_unrelated_progress(branch: Branch) -> None:
    change_plan(branch, "other")
    _ = (branch.project.project / "other.txt").write_text("New unrelated output")
    assert question(branch.project, branch.stage_id).id == branch.card.id


@pytest.mark.parametrize("name", ["target.txt", "base.txt"])
def test_related_or_upstream_file_change_requires_fresh_choice(branch: Branch, name: str) -> None:
    _ = (branch.project.project / name).write_text("Changed evidence")
    with pytest.raises(PlanError, match="已变化"):
        _ = answer(branch)


@pytest.mark.parametrize("key", ["base", "target"])
def test_related_or_upstream_proposal_change_requires_fresh_choice(
    branch: Branch, key: str
) -> None:
    change_plan(branch, key)
    with pytest.raises(PlanError, match="已变化"):
        _ = answer(branch)


def test_unrelated_lifecycle_progress_does_not_expire_choice(branch: Branch) -> None:
    other = next(spec.id for spec in branch.specs if spec.key == "other")
    with stage_store.connection(str(branch.project.project)) as database:
        _ = database.execute("UPDATE stages SET status='accepted' WHERE id=?", (other,))
    assert answer(branch).status == "answered"
