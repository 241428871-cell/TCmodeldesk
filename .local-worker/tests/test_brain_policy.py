"""The native permission snapshot selects one consistent tool and evidence policy."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from model_desk_control.native_commands import RECORD_EDIT_DENY

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import goal_actions
import goal_brain_contract
from goal_brain_contract import ACTION_SCHEMA, BrainError

if TYPE_CHECKING:
    from goal_brain_contract import Record

CONTEXT: Record = {"project": "/project", "goal": {"title": "Fixture"}}


def test_auto_brain_may_verify_but_not_edit_deliverables_or_records() -> None:
    text = goal_brain_contract.prompt(CONTEXT, "auto")
    assert "原生 auto" in text
    assert "reviewer_test" in text
    assert "不要直接编辑交付文件" in text
    assert ".model-desk" in text
    assert "你以只读权限运行" not in text
    assert RECORD_EDIT_DENY == "Edit(./.model-desk/**)"


def test_historical_read_only_snapshot_keeps_read_only_policy() -> None:
    text = goal_brain_contract.prompt(CONTEXT, "read_only")
    assert "你以只读权限运行" in text
    assert "原生 auto" not in text
    assert goal_brain_contract.prompt(CONTEXT) == text


def test_schema_offers_reviewer_test_evidence() -> None:
    assert "reviewer_test" in str(ACTION_SCHEMA)


@pytest.mark.parametrize("mode", ["read_only", "unknown"])
def test_read_only_brain_cannot_claim_its_own_test_run(mode: str) -> None:
    review: Record = {
        "task_id": "task",
        "decision": "accept",
        "criteria": [
            {
                "id": "c1",
                "status": "pass",
                "evidence": [{"source": "reviewer_test", "detail": "pytest -q", "exit_code": 0}],
                "note": "",
            }
        ],
    }
    with pytest.raises(BrainError, match="reviewer_test"):
        goal_actions.require_runnable_evidence(review, mode)
    goal_actions.require_runnable_evidence(review, "auto")
