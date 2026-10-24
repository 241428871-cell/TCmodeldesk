"""Historical review authority and damaged branch records cannot silently authorize work."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal, assert_never

import pytest
from pydantic import JsonValue, TypeAdapter

from model_desk_control.database import text_cursor

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import human_decisions
import plan_records
import stage_store
from plan_contracts import DecisionRequest, PlanError
from review_fixture import legacy
from test_decision_scope import Card, Fingerprint, answer, change_plan, question
from test_decision_scope import branch as branch
from test_decision_scope import goal as goal

if TYPE_CHECKING:
    from test_decision_scope import Branch

type Record = dict[str, JsonValue]


def test_old_card_keeps_whole_goal_scope_after_unrelated_file_change(branch: Branch) -> None:
    saved = legacy(str(branch.project.project), branch.project.goal_id, branch.card.id)
    card = branch.card.model_copy(
        update={
            "subject_hash": saved.subject_hash,
            "fingerprint": Fingerprint.model_validate(saved.fingerprint),
        }
    )
    _ = (branch.project.project / "other.txt").write_text("Different unrelated output")
    with pytest.raises(PlanError, match="已变化"):
        _ = answer(replace(branch, card=card))


def test_final_review_still_covers_unrelated_branches(branch: Branch) -> None:
    result = human_decisions.create_decision(
        str(branch.project.project),
        branch.project.goal_id,
        DecisionRequest(
            question="Accept all final artifacts?",
            reason="Final goal review",
            options=["Accept", "Revise"],
            approval_option="Accept",
            kind="final",
        ),
    )
    card = Card.model_validate_json(json.dumps(result))
    _ = (branch.project.project / "other.txt").write_text("Changed final output")
    with pytest.raises(PlanError, match="已变化"):
        _ = answer(replace(branch, card=card))


@pytest.mark.parametrize("field", ["reason", "question", "affected_stage_ids"])
def test_changed_saved_proposal_without_its_subject_is_rejected(branch: Branch, field: str) -> None:
    with stage_store.connection(str(branch.project.project)) as database:
        _ = database.execute(
            "UPDATE human_decisions SET payload=json_set(payload,?,?) WHERE id=?",
            ("$." + field, "Corrupt original proposal", branch.card.id),
        )
    with pytest.raises(PlanError, match="指纹不一致"):
        _ = answer(branch)


@pytest.mark.parametrize("column", ["payload", "fingerprint"])
def test_malformed_saved_json_is_a_typed_refusal(branch: Branch, column: str) -> None:
    with stage_store.connection(str(branch.project.project)) as database:
        if column == "payload":
            _ = database.execute(
                "UPDATE human_decisions SET payload='{' WHERE id=?", (branch.card.id,)
            )
        else:
            _ = database.execute(
                "UPDATE human_decisions SET fingerprint='{' WHERE id=?", (branch.card.id,)
            )
    with pytest.raises(PlanError, match="无法核对"):
        _ = answer(branch)


@pytest.mark.parametrize("defect", ["foreign", "missing", "retired", "cycle", "oversize"])
def test_unreadable_upstream_branch_refuses_old_answer(
    branch: Branch, defect: Literal["foreign", "missing", "retired", "cycle", "oversize"]
) -> None:
    base = next(spec.id for spec in branch.specs if spec.key == "base")
    with stage_store.connection(str(branch.project.project)) as database:
        match defect:
            case "foreign":
                _ = database.execute("UPDATE stages SET project='foreign' WHERE id=?", (base,))
            case "missing":
                _ = database.execute("DELETE FROM stages WHERE id=?", (base,))
            case "retired":
                _ = database.execute(
                    """UPDATE stages SET data=json_set(data,'$.dynamic_plan.superseded_by',2)
                    WHERE id=?""",
                    (base,),
                )
            case "cycle":
                _ = database.execute(
                    "UPDATE stages SET depends_on=? WHERE id=?",
                    (json.dumps([branch.stage_id]), base),
                )
            case "oversize":
                _ = database.execute(
                    "UPDATE stages SET objective=? WHERE id=?", ("x" * (2 * 1024 * 1024), base)
                )
            case _:
                assert_never(defect)
    with pytest.raises(PlanError, match="证据"):
        _ = answer(branch)


def test_unrelated_corrupt_record_does_not_block_a_branch_answer(branch: Branch) -> None:
    other = next(spec.id for spec in branch.specs if spec.key == "other")
    with stage_store.connection(str(branch.project.project)) as database:
        _ = database.execute("UPDATE stages SET data='{' WHERE id=?", (other,))
    assert answer(branch).status == "answered"


def test_unrelated_corrupt_record_does_not_block_a_new_branch_question(branch: Branch) -> None:
    other = next(spec.id for spec in branch.specs if spec.key == "other")
    with stage_store.connection(str(branch.project.project)) as database:
        _ = database.execute("UPDATE stages SET data='{' WHERE id=?", (other,))
    assert question(branch.project, branch.stage_id).id == branch.card.id


def rewrite_subject(branch: Branch, *, future: bool) -> None:
    with stage_store.connection(str(branch.project.project)) as database:
        row = text_cursor(
            database,
            "SELECT payload,fingerprint FROM human_decisions WHERE id=?",
            (branch.card.id,),
        ).fetchone()
        assert row is not None
        payload = TypeAdapter(Record).validate_json(row[0])
        fingerprint = TypeAdapter(Record).validate_json(row[1])
        if future:
            fingerprint["scope_version"] = 3
        else:
            payload["kind"] = "final"
        subject = plan_records.digest(
            {
                "goal_id": branch.project.goal_id,
                "proposal": payload,
                "fingerprint": fingerprint,
            }
        )
        _ = database.execute(
            "UPDATE human_decisions SET payload=?,fingerprint=?,subject_hash=? WHERE id=?",
            (plan_records.dump(payload), plan_records.dump(fingerprint), subject, branch.card.id),
        )


@pytest.mark.parametrize("future", [True, False])
def test_future_version_or_final_branch_cannot_fall_back_to_legacy(
    branch: Branch, *, future: bool
) -> None:
    rewrite_subject(branch, future=future)
    with pytest.raises(PlanError, match=r"无法核对|最终审核不能"):
        _ = answer(branch)


def test_oversize_saved_proposal_is_refused_before_json_decoding(branch: Branch) -> None:
    with stage_store.connection(str(branch.project.project)) as database:
        _ = database.execute(
            "UPDATE human_decisions SET payload=? WHERE id=?", ("x" * 262145, branch.card.id)
        )
    with pytest.raises(PlanError, match="超过读取预算"):
        _ = answer(branch)


def test_denied_answer_still_requires_replanning_that_branch(branch: Branch) -> None:
    denied = branch.card.model_copy(update={"options": tuple(reversed(branch.card.options))})
    assert answer(replace(branch, card=denied)).status == "answered"
    pending = human_decisions.pending_for_stage(str(branch.project.project), branch.stage_id)
    assert len(pending) == 1
    change_plan(branch, "target")
    assert human_decisions.pending_for_stage(str(branch.project.project), branch.stage_id) == []
