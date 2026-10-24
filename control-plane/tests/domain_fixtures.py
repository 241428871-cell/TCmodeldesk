"""Temporary trusted domain fixtures; never load accounts or invoke a native provider."""

from pathlib import Path

from model_desk_control.control import Control
from model_desk_control.decision_store import DecisionStore
from model_desk_control.decisions import (
    AnswerDecision,
    Decision,
    DecisionOption,
    DecisionProposal,
    ExceptionReason,
    HumanAnswer,
    RaiseDecision,
)
from model_desk_control.plan_store import PatchCommand, PlanStore
from model_desk_control.plans import PlanPatch, TaskSpec
from model_desk_control.store import ControlStore


def task(task_id: str, *, dependencies: tuple[str, ...] = ()) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        objective=f"Deliver {task_id}",
        acceptance=("A verified artifact exists",),
        dependencies=dependencies,
    )


def domain_at(directory: Path) -> ControlStore:
    store = ControlStore(directory / "domain.sqlite")
    store.initialize()
    store.create(Control(goal_id="project/goal", due_at=100))
    _ = PlanStore(store).patch(
        PatchCommand(
            goal_id="project/goal",
            request_id="initial-plan",
            epoch=0,
            control_revision=0,
            patch=PlanPatch(
                base_revision=0,
                reason="Independent tasks and a dependent task",
                added=(task("a"), task("b"), task("c", dependencies=("a",))),
            ),
        )
    )
    return store


def raise_question(store: ControlStore, *, request_id: str = "question") -> Decision:
    return DecisionStore(store).raise_exception(
        RaiseDecision(
            goal_id="project/goal",
            request_id=request_id,
            epoch=0,
            control_revision=0,
            proposal=DecisionProposal(
                task_id="a",
                reason=ExceptionReason.MISSING_FACT,
                question="Which target environment should this task support?",
                evidence=("The supplied requirements do not identify the environment.",),
                options=(DecisionOption(option_id="local", label="Local environment"),),
                recommendation="local",
            ),
        )
    )


def answer_for(decision: Decision, *, request_id: str = "answer") -> AnswerDecision:
    return AnswerDecision(
        goal_id=decision.scope.goal_id,
        request_id=request_id,
        epoch=0,
        control_revision=0,
        decision_id=decision.decision_id,
        decision_revision=decision.revision,
        answer=HumanAnswer(actor_id="human", selected_option="local", answered_at=101),
    )
