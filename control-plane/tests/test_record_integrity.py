"""Database documents must agree with their trusted relational identities."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from model_desk_control.control import UserIntent
from model_desk_control.database import StoreError
from model_desk_control.decision_store import DecisionStore
from model_desk_control.decisions import DecisionStatus
from model_desk_control.plan_store import PlanStore
from model_desk_control.task_state import read_task
from tests.domain_fixtures import domain_at, raise_question


def test_control_json_cannot_change_goal_identity(tmp_path: Path) -> None:
    controls = domain_at(tmp_path)
    wrong = controls.read("project/goal").model_copy(update={"goal_id": "other/goal"})
    with controls.connection() as database, database:
        _ = database.execute("UPDATE controls SET document = ?", (wrong.model_dump_json(),))
    with pytest.raises(StoreError, match="control_record_identity_mismatch"):
        _ = controls.read("project/goal")


def test_plan_document_revision_must_match_immutable_row(tmp_path: Path) -> None:
    controls = domain_at(tmp_path)
    wrong = PlanStore(controls).read("project/goal").model_copy(update={"revision": 99})
    with controls.connection() as database, database:
        _ = database.execute(
            "UPDATE plans SET document = ? WHERE revision = 1", (wrong.model_dump_json(),)
        )
    with pytest.raises(StoreError, match="plan_record_revision_mismatch"):
        _ = PlanStore(controls).read("project/goal")


def test_task_document_cannot_alias_other_task(tmp_path: Path) -> None:
    controls = domain_at(tmp_path)
    with controls.connection() as database, database:
        wrong = read_task(database, "project/goal", "a").model_copy(update={"task_id": "b"})
        _ = database.execute(
            "UPDATE task_states SET document = ? WHERE task_id = 'a'", (wrong.model_dump_json(),)
        )
    with pytest.raises(StoreError, match="task_record_identity_mismatch"):
        _ = PlanStore(controls).ready("project/goal")


def test_decision_document_cannot_move_to_other_goal(tmp_path: Path) -> None:
    controls = domain_at(tmp_path)
    decision = raise_question(controls)
    wrong = decision.model_copy(
        update={"scope": decision.scope.model_copy(update={"goal_id": "other/goal"})}
    )
    with controls.connection() as database, database:
        _ = database.execute("UPDATE decisions SET document = ?", (wrong.model_dump_json(),))
    with pytest.raises(StoreError, match="decision_record_identity_mismatch"):
        _ = DecisionStore(controls).read("project/goal", decision.decision_id)
    with pytest.raises(StoreError, match="decision_record_identity_mismatch"):
        _ = DecisionStore(controls).pending("project/goal")


def test_decision_cannot_claim_consumption_without_an_answer(tmp_path: Path) -> None:
    controls = domain_at(tmp_path)
    decision = raise_question(controls)
    wrong = decision.model_copy(update={"status": DecisionStatus.CONSUMED})
    with controls.connection() as database, database:
        _ = database.execute("UPDATE decisions SET document = ?", (wrong.model_dump_json(),))
    with pytest.raises(ValidationError, match="inconsistent_decision_record"):
        _ = DecisionStore(controls).read("project/goal", decision.decision_id)


def test_stop_does_not_depend_on_parsing_damaged_decision_or_task(tmp_path: Path) -> None:
    controls = domain_at(tmp_path)
    _ = raise_question(controls)
    _ = controls.claim(goal_id="project/goal", request_id="tick", epoch=0, revision=0, now=100)
    with controls.connection() as database, database:
        _ = database.execute("UPDATE decisions SET document = '{\"invalid\": true}'")
        _ = database.execute("UPDATE task_states SET document = '{\"invalid\": true}'")
    stopped = controls.stop(goal_id="project/goal", request_id="urgent-stop")
    assert stopped.intent is UserIntent.STOP
    assert stopped.busy  # Native exit has not been observed; revocation does not invent one.
    assert controls.pending_count("project/goal") == 0
    assert DecisionStore(controls).pending("project/goal") == ()
