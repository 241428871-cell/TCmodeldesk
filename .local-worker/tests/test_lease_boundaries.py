"""Leases must not bypass original admission, STOP or canonical file ownership."""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from model_desk_control.resource_leases import LeaseBroker, LeaseRequest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import continuation_clock
import execution_leases
import model_registry as registry
import panel_server as panel
import task_queue
import task_queue_driver
import workspace_config
from test_goal_action_receipts import GoalRun, apply, proposal
from test_goal_action_receipts import run as run
from test_task_queue import capture, process

if TYPE_CHECKING:
    import pytest
    from goal_brain_contract import Record


def test_invalid_concurrency_config_cannot_bypass_a_lease(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    callbacks = capture(monkeypatch)
    task_id = apply(run, proposal(run)).dispatched_task_ids[0]
    launches = process(monkeypatch, run, task_id)

    def invalid(_project: str) -> int:
        message = "invalid concurrency fixture"
        raise workspace_config.WorkspaceError(message)

    monkeypatch.setattr(workspace_config, "work_concurrency", invalid)
    callbacks[0]()
    assert launches == []
    assert registry.read_task(registry.task_dir(task_id))["status"] == "recovery_pending"


def test_stopped_goal_exits_lease_wait_without_waiting_for_unrelated_worker(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    callbacks = capture(monkeypatch)
    task_id = apply(run, proposal(run)).dispatched_task_ids[0]
    launches = process(monkeypatch, run, task_id)
    broker = LeaseBroker()
    monkeypatch.setattr(execution_leases, "BROKER", broker)
    blocker = LeaseRequest(owner="unrelated", project=run.scope.project, account="fixture")
    broker.acquire(blocker, lambda: False)
    entered = threading.Event()
    acquire = execution_leases.acquire

    def observe(request: LeaseRequest, directory: Path) -> None:
        entered.set()
        acquire(request, directory)

    monkeypatch.setattr(execution_leases, "acquire", observe)
    worker = threading.Thread(target=callbacks[0], daemon=True)
    worker.start()
    try:
        assert entered.wait(3)
        task_queue_driver.recover(
            run.scope.project, run.scope.goal_id, panel.EXECUTOR, panel.execute_task
        )
        assert len(callbacks) == 1
        _ = continuation_clock.control(run.scope.project, run.scope.goal_id, "stop")
        worker.join(2)
        assert not worker.is_alive()
        assert launches == []
        assert broker.state().waiting == ()
        receipt = task_queue.observed(registry.read_task(registry.task_dir(task_id)))
        assert receipt is not None
        assert receipt.phase.value == "blocked"
    finally:
        broker.release(blocker.owner)
        worker.join(3)


def test_symlinked_file_names_share_a_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path.resolve()
    (project / "real").mkdir()
    (project / "alias").symlink_to(project / "real", target_is_directory=True)

    def limit(_project: str) -> int:
        return 2

    monkeypatch.setattr(workspace_config, "work_concurrency", limit)
    state: Record = {"id": "first", "project": str(project), "kind": "edit", "files": ["real/a.py"]}
    model: Record = {"provider": "local", "id": "fixture"}
    first = execution_leases.request_for(state, model)
    second = execution_leases.request_for({**state, "id": "second", "files": ["alias/a.py"]}, model)
    assert first.files == second.files


def test_account_change_during_admission_never_launches_under_old_lease(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    callbacks = capture(monkeypatch)
    task_id = apply(run, proposal(run)).dispatched_task_ids[0]
    launches = process(monkeypatch, run, task_id)
    original_model = registry.get_model
    acquire = execution_leases.acquire

    def changed_model(model_id: str, *, require_enabled: bool = True) -> Record:
        model = original_model(model_id, require_enabled=require_enabled)
        return {**model, "provider": "claude_cli"}

    def change_after_acquire(request: LeaseRequest, directory: Path) -> None:
        acquire(request, directory)
        monkeypatch.setattr(registry, "get_model", changed_model)

    monkeypatch.setattr(execution_leases, "acquire", change_after_acquire)
    callbacks[0]()
    assert launches == []
    saved = registry.read_task(registry.task_dir(task_id))
    assert "资源身份" in str(saved.get("recovery"))
    assert execution_leases.state().held == ()


def test_cancelled_admission_cannot_launch_if_authority_is_allowed_again(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    callbacks = capture(monkeypatch)
    task_id = apply(run, proposal(run)).dispatched_task_ids[0]
    launches = process(monkeypatch, run, task_id)

    def cancelled(_request: LeaseRequest, _directory: Path) -> None:
        raise execution_leases.LeaseCancelled

    monkeypatch.setattr(execution_leases, "acquire", cancelled)
    callbacks[0]()
    assert launches == []
    saved = registry.read_task(registry.task_dir(task_id))
    assert "未取得执行租约" in str(saved.get("recovery"))
