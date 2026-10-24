"""Scoped answers cannot authorize another task, changed artifact or stopped goal."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from model_desk_control.control import ContractError, Control
from model_desk_control.database import StoreError
from model_desk_control.decision_store import DecisionStore
from model_desk_control.decisions import ConsumeDecision, Decision, DecisionStatus, RaiseDecision
from model_desk_control.plan_store import PlanStore
from model_desk_control.plans import PlanPatch
from model_desk_control.store import ControlStore
from model_desk_control.task_state import TaskStatus, read_task, write_task
from tests.domain_fixtures import answer_for, domain_at, raise_question
from tests.test_plans_persistence import add_command


def consume_for(decision: Decision, *, request_id: str = "consume") -> ConsumeDecision:
    return ConsumeDecision(
        goal_id=decision.scope.goal_id,
        request_id=request_id,
        epoch=0,
        control_revision=0,
        task_id=decision.scope.task_id,
        decision_id=decision.decision_id,
        decision_revision=decision.revision,
    )


def test_duplicate_question_and_answer_survive_reopen(tmp_path: Path) -> None:
    controls = domain_at(tmp_path)
    first = raise_question(controls)
    assert raise_question(controls, request_id="second-question") == first
    decisions = DecisionStore(ControlStore(controls.path))
    assert decisions.pending("project/goal") == (first,)
    answered = decisions.answer(answer_for(first))
    assert decisions.answer(answer_for(first)) == answered
    assert answered.status is DecisionStatus.ANSWERED
    assert decisions.read("project/goal", first.decision_id) == answered


def test_waiting_task_does_not_block_independent_task(tmp_path: Path) -> None:
    controls = domain_at(tmp_path)
    plans = PlanStore(controls)
    assert plans.ready("project/goal") == ("a", "b")
    decision = raise_question(controls)
    assert plans.ready("project/goal") == ("b",)
    decisions = DecisionStore(controls)
    answered = decisions.answer(answer_for(decision))
    assert plans.ready("project/goal") == ("b",)
    consumed = decisions.consume(consume_for(answered))
    assert consumed.status is DecisionStatus.CONSUMED
    assert plans.ready("project/goal") == ("a", "b")
    assert decisions.consume(consume_for(answered)) == consumed
    with pytest.raises(StoreError, match="decision_revision_conflict"):
        _ = decisions.consume(consume_for(answered, request_id="duplicate-consumer"))


def test_unrelated_plan_revision_keeps_answer_and_question_identity(tmp_path: Path) -> None:
    controls = domain_at(tmp_path)
    decision = raise_question(controls)
    decisions = DecisionStore(controls)
    answered = decisions.answer(answer_for(decision))
    assert PlanStore(controls).patch(add_command("d")).revision == 2
    assert raise_question(controls, request_id="after-replan") == answered
    assert decisions.reconcile("project/goal") == 0
    assert decisions.consume(consume_for(answered)).answer == answered.answer


def test_changed_artifact_revokes_answer_without_losing_history(tmp_path: Path) -> None:
    controls = domain_at(tmp_path)
    decisions = DecisionStore(controls)
    answered = decisions.answer(answer_for(raise_question(controls)))
    with controls.connection() as database, database:
        state = read_task(database, "project/goal", "a")
        write_task(
            database,
            "project/goal",
            state.model_copy(update={"artifact_hash": "a" * 64, "context_revision": 1}),
        )
    with pytest.raises(StoreError, match="artifact_changed"):
        _ = decisions.consume(consume_for(answered))
    assert decisions.pending("project/goal") == ()
    assert decisions.reconcile("project/goal") == 1
    invalidated = decisions.read("project/goal", answered.decision_id)
    assert invalidated.status is DecisionStatus.INVALIDATED
    assert invalidated.invalid_reason == "artifact_changed"
    assert invalidated.answer == answered.answer
    assert decisions.reconcile("project/goal") == 0


def test_superseding_task_invalidates_card_in_same_transaction(tmp_path: Path) -> None:
    controls = domain_at(tmp_path)
    decision = raise_question(controls)
    command = add_command("replacement").model_copy(
        update={"patch": PlanPatch(base_revision=1, reason="New route", superseded=("a", "c"))}
    )
    _ = PlanStore(controls).patch(command)
    invalidated = DecisionStore(controls).read("project/goal", decision.decision_id)
    assert invalidated.status is DecisionStatus.INVALIDATED
    assert invalidated.invalid_reason == "task_superseded"
    assert PlanStore(controls).ready("project/goal") == ("b",)


def test_stop_invalidates_card_and_blocks_unconsumed_answer(tmp_path: Path) -> None:
    controls = domain_at(tmp_path)
    decisions = DecisionStore(controls)
    answered = decisions.answer(answer_for(raise_question(controls)))
    _ = controls.stop(goal_id="project/goal", request_id="stop")
    assert decisions.read("project/goal", answered.decision_id).status is DecisionStatus.INVALIDATED
    with pytest.raises(ContractError, match="stale_authority_or_revision"):
        _ = decisions.consume(consume_for(answered))
    assert decisions.pending("project/goal") == ()


def test_answer_cannot_move_to_other_task_or_goal(tmp_path: Path) -> None:
    controls = domain_at(tmp_path)
    decisions = DecisionStore(controls)
    answered = decisions.answer(answer_for(raise_question(controls)))
    with pytest.raises(StoreError, match="decision_task_mismatch"):
        _ = decisions.consume(consume_for(answered).model_copy(update={"task_id": "b"}))
    controls.create(Control(goal_id="other/goal", due_at=100))
    with pytest.raises(StoreError, match="decision_not_found"):
        _ = decisions.read("other/goal", answered.decision_id)
    with pytest.raises(StoreError, match="decision_not_found"):
        _ = decisions.consume(consume_for(answered).model_copy(update={"goal_id": "other/goal"}))


def test_two_human_answers_compete_for_one_card_revision(tmp_path: Path) -> None:
    controls = domain_at(tmp_path)
    decision = raise_question(controls)

    def compete(identity: str) -> str:
        try:
            _ = DecisionStore(ControlStore(controls.path)).answer(
                answer_for(decision, request_id=identity)
            )
        except StoreError as error:
            return error.code
        return "answered"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(compete, ("human-a", "human-b")))
    assert sorted(outcomes) == ["answered", "decision_revision_conflict"]


def test_unknown_native_work_remains_unknown_after_human_answer(tmp_path: Path) -> None:
    controls = domain_at(tmp_path)
    with controls.connection() as database, database:
        state = read_task(database, "project/goal", "a")
        write_task(
            database, "project/goal", state.model_copy(update={"status": TaskStatus.UNKNOWN})
        )
    decisions = DecisionStore(controls)
    answered = decisions.answer(answer_for(raise_question(controls)))
    _ = decisions.consume(consume_for(answered))
    with controls.connection() as database:
        assert read_task(database, "project/goal", "a").status is TaskStatus.UNKNOWN
    assert PlanStore(controls).ready("project/goal") == ("b",)


def test_multiple_questions_only_release_task_after_all_answers_consumed(tmp_path: Path) -> None:
    controls = domain_at(tmp_path)
    first = raise_question(controls)
    decisions = DecisionStore(controls)
    second = decisions.raise_exception(
        RaiseDecision(
            goal_id="project/goal",
            request_id="different-question",
            epoch=0,
            control_revision=0,
            proposal=first.proposal.model_copy(update={"question": "Which output format?"}),
        )
    )
    _ = decisions.consume(consume_for(decisions.answer(answer_for(first))))
    assert PlanStore(controls).ready("project/goal") == ("b",)
    answered = decisions.answer(answer_for(second, request_id="second-answer"))
    _ = decisions.consume(consume_for(answered, request_id="second-consume"))
    assert PlanStore(controls).ready("project/goal") == ("a", "b")


def test_unoffered_choice_is_rejected_without_consuming_request(tmp_path: Path) -> None:
    controls = domain_at(tmp_path)
    decisions = DecisionStore(controls)
    decision = raise_question(controls)
    command = answer_for(decision)
    invalid = command.model_copy(
        update={"answer": command.answer.model_copy(update={"selected_option": "invented"})}
    )
    with pytest.raises(StoreError, match="answer_option_not_available"):
        _ = decisions.answer(invalid)
    assert decisions.answer(command).status is DecisionStatus.ANSWERED
