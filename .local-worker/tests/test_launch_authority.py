"""Brain and DSH/Pi processes recheck live authority at the real process boundary."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import continuation_clock
import dynamic_plan
import goal_brain_context
import goal_controller
import governance_context
import panel_server as panel
import test_goal_controller as fixtures
import workflow_settings
from governance_context import DispatchBlocked

if TYPE_CHECKING:
    from collections.abc import Generator

    from goal_brain_context import Selection
    from goal_brain_contract import Record


@pytest.fixture
def case() -> Generator[fixtures.GoalControllerTests]:
    legacy = fixtures.GoalControllerTests()
    legacy.setUp()
    try:
        yield legacy
    finally:
        legacy.doCleanups()


def revoke_paid() -> None:
    _ = workflow_settings.save_settings(
        {
            "enabled": True,
            "paid_allowed": False,
            "enforce_paid_budget": False,
            "enforce_model_call_limit": False,
        }
    )


def test_paid_revocation_after_snapshot_prevents_the_brain_process(
    case: fixtures.GoalControllerTests, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = goal_brain_context.build_context

    def build_then_revoke(project: str, goal_id: str, selection: Selection) -> Record:
        context = built(project, goal_id, selection)
        revoke_paid()
        return context

    monkeypatch.setattr(goal_controller, "build_context", build_then_revoke)
    popen = mock.Mock(side_effect=AssertionError("brain process must not start"))
    monkeypatch.setattr(subprocess, "Popen", popen)
    result = goal_controller.run_once(case.project_text, case.goal, panel)
    assert result is not None
    popen.assert_not_called()


def task_state(case: fixtures.GoalControllerTests, epoch: int) -> Record:
    return {
        "id": "task",
        "project": case.project_text,
        "goal_id": case.goal,
        "authority_epoch": epoch,
        "kind": "chat",
        "files": [],
        "goal_contract_revision": int(
            dynamic_plan.describe_goal(case.project_text, case.goal)["contract_revision"]
        ),
    }


def test_stopped_goal_refuses_the_dsh_or_pi_worker_process(
    case: fixtures.GoalControllerTests, tmp_path: Path
) -> None:
    epoch = continuation_clock.snapshot(case.project_text, case.goal)["authority_epoch"]
    _ = continuation_clock.control(case.project_text, case.goal, "stop")
    with (
        mock.patch.object(subprocess, "Popen") as popen,
        pytest.raises(ValueError, match="STOP"),
    ):
        _ = governance_context.fenced_popen(tmp_path, task_state(case, epoch), ["worker"])
    popen.assert_not_called()


def test_cancelled_task_refuses_the_worker_process(
    case: fixtures.GoalControllerTests, tmp_path: Path
) -> None:
    epoch = continuation_clock.snapshot(case.project_text, case.goal)["authority_epoch"]
    _ = (tmp_path / "cancelled.flag").write_text("", encoding="utf-8")
    with (
        mock.patch.object(subprocess, "Popen") as popen,
        pytest.raises(DispatchBlocked, match="已取消"),
    ):
        _ = governance_context.fenced_popen(tmp_path, task_state(case, epoch), ["worker"])
    popen.assert_not_called()


def test_current_authority_starts_the_worker_once(
    case: fixtures.GoalControllerTests, tmp_path: Path
) -> None:
    epoch = continuation_clock.snapshot(case.project_text, case.goal)["authority_epoch"]
    with mock.patch.object(subprocess, "Popen") as popen:
        _ = governance_context.fenced_popen(tmp_path, task_state(case, epoch), ["worker"])
    popen.assert_called_once()
