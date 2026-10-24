"""An answer authorizes one original stage dispatch, not unlimited future work."""

from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from model_desk_control.host_decision_uses import read_use

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import governance_context
import human_decisions
import stage_store
from plan_contracts import DecisionRequest, PlanError
from review_fixture import legacy
from test_decision_scope import Card, answer, question
from test_decision_scope import branch as branch
from test_decision_scope import goal as goal

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from pydantic import JsonValue
    from test_decision_scope import Branch


def approve(branch: Branch) -> Branch:
    base = next(spec.id for spec in branch.specs if spec.key == "base")
    with stage_store.connection(str(branch.project.project)) as database:
        _ = database.execute("UPDATE stages SET status='accepted' WHERE id=?", (base,))
    current = replace(branch, card=question(branch.project, branch.stage_id))
    assert answer(current).status == "answered"
    return current


def claim(branch: Branch, task: str) -> None:
    _ = stage_store.claim_dispatch(branch.stage_id, task, project=str(branch.project.project))


def authority(branch: Branch, task: str) -> AbstractContextManager[None]:
    return governance_context.launch_authority(
        {
            "id": task,
            "project": str(branch.project.project),
            "goal_id": branch.project.goal_id,
            "stage_id": branch.stage_id,
            "authority_epoch": branch.card.fingerprint.authority_epoch,
            "goal_contract_revision": 1,
            "kind": "edit",
            "files": ["target.txt"],
        }
    )


def test_related_file_change_after_answer_blocks_actual_dispatch(branch: Branch) -> None:
    current = approve(branch)
    _ = (branch.project.project / "target.txt").write_text("Changed after the human answered")
    with pytest.raises(PlanError, match="审核"):
        claim(current, "task_first")


def test_approved_question_cannot_be_reused_for_another_task(branch: Branch) -> None:
    current = approve(branch)
    claim(current, "task_first")
    with stage_store.connection(str(branch.project.project)) as database:
        _ = database.execute(
            "UPDATE stages SET status='pending',active_task_id='' WHERE id=?", (branch.stage_id,)
        )
    with pytest.raises(PlanError, match="审核"):
        claim(current, "task_second")


def test_original_task_can_write_output_but_not_execute_a_replaced_proposal(branch: Branch) -> None:
    current = approve(branch)
    claim(current, "task_first")
    state: dict[str, JsonValue] = {
        "id": "task_first",
        "project": str(branch.project.project),
        "goal_id": branch.project.goal_id,
        "stage_id": branch.stage_id,
        "authority_epoch": current.card.fingerprint.authority_epoch,
        "goal_contract_revision": 1,
        "kind": "edit",
        "files": ["target.txt"],
    }
    with governance_context.launch_authority(state):
        _ = (branch.project.project / "target.txt").write_text("Output of the approved task")
    with stage_store.connection(str(branch.project.project)) as database:
        _ = database.execute(
            "UPDATE stages SET objective='Replaced while queued' WHERE id=?", (branch.stage_id,)
        )
    with (
        pytest.raises((PlanError, governance_context.DispatchBlocked), match="审核"),
        governance_context.launch_authority(state),
    ):
        pytest.fail("The replaced proposal cannot inherit the original task's approval")


def test_one_question_can_authorize_independent_stages_once_each(branch: Branch) -> None:
    selected = [spec.id for spec in branch.specs if spec.key in {"base", "other"}]
    result = human_decisions.create_decision(
        str(branch.project.project),
        branch.project.goal_id,
        DecisionRequest(
            question="Accept these two independent proposals?",
            reason="Related human choice",
            options=["Accept", "Revise"],
            approval_option="Accept",
            affected_stage_ids=selected,
        ),
    )
    current = replace(branch, card=Card.model_validate_json(json.dumps(result)))
    assert answer(current).status == "answered"
    claim(replace(current, stage_id=selected[0]), "task_base")
    _ = (branch.project.project / "base.txt").write_text("First independent task made progress")
    claim(replace(current, stage_id=selected[1]), "task_other")


def test_failed_stage_claim_rolls_back_approval_consumption(branch: Branch) -> None:
    current = approve(branch)
    with stage_store.connection(str(branch.project.project)) as database:
        _ = database.execute("""CREATE TRIGGER fail_dispatch BEFORE UPDATE OF status ON stages
            WHEN NEW.status='dispatched'
            BEGIN SELECT RAISE(ABORT,'injected_dispatch_failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="injected_dispatch_failure"):
        claim(current, "task_first")
    with stage_store.connection(str(branch.project.project)) as database:
        assert read_use(database, current.card.id, current.stage_id) is None
        _ = database.execute("DROP TRIGGER fail_dispatch")
    claim(current, "task_first")
    with stage_store.connection(str(branch.project.project)) as database:
        use = read_use(database, current.card.id, current.stage_id)
        assert use is not None
        assert use.task_id == "task_first"


def test_same_claim_can_replay_without_a_second_use(branch: Branch) -> None:
    current = approve(branch)
    claim(current, "task_first")
    with stage_store.connection(str(branch.project.project)) as database:
        original = read_use(database, current.card.id, current.stage_id)
    claim(current, "task_first")
    with stage_store.connection(str(branch.project.project)) as database:
        assert read_use(database, current.card.id, current.stage_id) == original


def test_legacy_answer_must_still_match_the_whole_goal_at_dispatch(branch: Branch) -> None:
    current = approve(branch)
    _ = legacy(str(branch.project.project), branch.project.goal_id, current.card.id)
    _ = (branch.project.project / "other.txt").write_text("Changed unrelated legacy evidence")
    with pytest.raises(PlanError, match="整目标审核已失效"):
        claim(current, "task_first")


def test_fresh_question_supersedes_stale_answer_without_erasing_history(branch: Branch) -> None:
    current = approve(branch)
    _ = (branch.project.project / "target.txt").write_text("Revised before dispatch")
    renewed = replace(current, card=question(branch.project, branch.stage_id))
    assert renewed.card.id != current.card.id
    assert answer(renewed).status == "answered"
    claim(renewed, "task_after_review")
    cards = human_decisions.list_decisions(str(branch.project.project))
    old = next(card for card in cards if card["id"] == current.card.id)
    assert old["status"] == "stale"
    assert old["answer"] == current.card.options[0].id


def test_upstream_change_blocks_the_consumed_task_before_launch(branch: Branch) -> None:
    current = approve(branch)
    claim(current, "task_first")
    _ = (branch.project.project / "base.txt").write_text("Changed prerequisite evidence")
    with pytest.raises(PlanError, match="审核"), authority(current, "task_first"):
        pytest.fail("Changed upstream evidence must prevent the old task from launching")


def test_use_cannot_be_transferred_to_another_task_at_launch(branch: Branch) -> None:
    current = approve(branch)
    claim(current, "task_first")
    with pytest.raises(PlanError, match="已用于原任务"), authority(current, "task_impostor"):
        pytest.fail("A different task cannot use this receipt")


def test_launch_cannot_create_a_missing_use(branch: Branch) -> None:
    current = approve(branch)
    with pytest.raises(PlanError, match="缺少审核消费记录"), authority(current, "task_first"):
        pytest.fail("Only the atomic stage claim may consume an approval")
    with stage_store.connection(str(branch.project.project)) as database:
        assert read_use(database, current.card.id, current.stage_id) is None


def test_corrupt_use_identity_is_not_accepted_on_replay(branch: Branch) -> None:
    current = approve(branch)
    claim(current, "task_first")
    with stage_store.connection(str(branch.project.project)) as database:
        _ = database.execute(
            """UPDATE human_decision_uses SET data=json_set(data,'$.task_id','foreign_task')
            WHERE decision_id=?""",
            (current.card.id,),
        )
    with pytest.raises(PlanError, match="无法核对"), authority(current, "task_first"):
        pytest.fail("Stored use identity must match its SQL association")


def test_only_explicit_claim_can_rebind_the_original_task_after_recovery(branch: Branch) -> None:
    current = approve(branch)
    claim(current, "task_first")
    project = str(branch.project.project)
    _ = stage_store.mark_recovery_pending(branch.stage_id, "Inspect original task", project=project)
    _ = stage_store.requeue_stage(
        branch.stage_id, "Explicit original-task recovery", project=project
    )
    with pytest.raises(PlanError, match="不一致"), authority(current, "task_first"):
        pytest.fail("The native worker cannot reclaim a stage itself")
    claim(current, "task_first")
    with authority(current, "task_first"):
        assert (branch.project.project / "target.txt").read_text() == "target"
