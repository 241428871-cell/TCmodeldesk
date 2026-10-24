"""A real local child cannot write back after its original goal authority is revoked."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from pydantic import TypeAdapter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cli_worker
import continuation_clock
import goal_actions
import goal_file_scope
import model_registry as registry
import panel_server as panel
from test_goal_action_receipts import proposal
from test_goal_action_receipts import run as run

if TYPE_CHECKING:
    from test_goal_action_receipts import GoalRun

    from model_desk_control.file_authority import FileScope


def test_stop_after_native_result_prevents_original_file_writeback(run: GoalRun) -> None:
    _ = goal_actions.apply(
        run.scope.project,
        run.scope.goal_id,
        run.scope.epoch,
        proposal(run, edit=True),
        panel,
        {},
        run.scope.project + "/saved-actions.json",
        scope=run.scope,
    )
    task = registry.task_dir(run.queued[0])
    recorded = registry.read_task(task)
    state: cli_worker.NativeTask = {
        "engine": "codex_cli",
        "kind": "edit",
        "project": run.scope.project,
        "files": ["generated.txt"],
        "timeout": 30,
        "goal_id": run.scope.goal_id,
        "stage_id": TypeAdapter(str).validate_python(recorded["stage_id"]),
        "authority_epoch": run.scope.epoch,
        "goal_contract_revision": 1,
        "automatic_goal": True,
    }
    actual = goal_file_scope.validate_files
    selected_calls = 0

    def revoke(project: str, mode: FileScope, allowed: list[str], files: list[str]) -> list[str]:
        nonlocal selected_calls
        result = actual(project, mode, allowed, files)
        if mode == "selected":
            selected_calls += 1
            if selected_calls == 2:
                _ = continuation_clock.control(run.scope.project, run.scope.goal_id, "stop")
        return result

    script = (
        "import sys;from pathlib import Path;sys.stdin.read();"
        "Path('generated.txt').write_text('new output');print('created')"
    )
    with (
        patch.object(cli_worker, "command", return_value=[sys.executable, "-c", script]),
        patch.object(goal_file_scope, "validate_files", side_effect=revoke),
        pytest.raises(ValueError, match="STOP"),
    ):
        cli_worker.run_task(task, state, {"provider": "codex_cli", "model": "fixture-model"})

    assert not (Path(run.scope.project) / "generated.txt").exists()
    output = next((task / "native-cli").glob("*/native-output.txt"))
    assert "created" in output.read_text()
    assert (
        continuation_clock.snapshot(run.scope.project, run.scope.goal_id)["authority_epoch"]
        > run.scope.epoch
    )
