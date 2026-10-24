"""Interrupted admitted tasks resume their original inputs without rerouting or duplication."""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from model_desk_control.database import text_cursor

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import continuation_clock
import governance_context
import model_registry as registry
import model_router
import stage_store
import task_queue
from test_goal_action_receipts import GoalRun, apply, proposal
from test_goal_action_receipts import run as run
from test_task_queue import capture

if TYPE_CHECKING:
    from collections.abc import Generator

    from goal_brain_contract import Record


def fail_preparation(_task_id: str, _project: str) -> Path:
    message = "Fixture cannot initialize original task directory"
    raise OSError(message)


def original_task(run: GoalRun) -> str:
    with stage_store.connection(run.scope.project) as database:
        row = text_cursor(
            database, "SELECT active_task_id FROM stages WHERE goal_id=?", (run.scope.goal_id,)
        ).fetchone()
    assert row is not None
    return row[0]


def test_first_directory_failure_recovers_the_same_admitted_task(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    callbacks = capture(monkeypatch)
    prepare = registry.prepare_task_dir
    calls: list[str] = []

    def interrupted(task_id: str, project: str) -> Path:
        calls.append(task_id)
        if len(calls) == 1:
            return fail_preparation(task_id, project)
        return prepare(task_id, project)

    monkeypatch.setattr(registry, "prepare_task_dir", interrupted)
    result = apply(run, proposal(run))
    assert result.dispatched_task_ids == (original_task(run),)
    assert len(set(calls)) == 1
    assert task_queue.observed(registry.read_task(registry.task_dir(calls[0]))) is not None
    assert callbacks == []


def test_later_recovery_uses_original_model_and_task_id(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare = registry.prepare_task_dir
    monkeypatch.setattr(registry, "prepare_task_dir", fail_preparation)
    actions = proposal(run)
    with pytest.raises(ValueError, match="未知"):
        _ = apply(run, actions)
    task_id = original_task(run)
    monkeypatch.setattr(registry, "prepare_task_dir", prepare)

    def no_routing(*_args: str, **_kwargs: str) -> None:
        message = "Recovery must not select a new model or recreate a task"
        raise AssertionError(message)

    monkeypatch.setattr(model_router, "choose", no_routing)
    result = apply(run, actions)
    assert result.dispatched_task_ids == (task_id,)
    assert registry.read_task(registry.task_dir(task_id))["model_id"] == run.model_id


def test_queue_preparation_failure_recovers_initialized_files_once(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare = task_queue.prepare
    attempts: list[str] = []

    def interrupted(state: Record) -> None:
        attempts.append(str(state["id"]))
        if len(attempts) == 1:
            message = "Fixture lost queue preparation before commit"
            raise OSError(message)
        prepare(state)

    monkeypatch.setattr(task_queue, "prepare", interrupted)
    result = apply(run, proposal(run))
    assert len(result.dispatched_task_ids) == 1
    assert len(set(attempts)) == 1
    assert len(attempts) == 2


def test_stop_prevents_materialization_of_a_previously_claimed_task(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare = registry.prepare_task_dir
    monkeypatch.setattr(registry, "prepare_task_dir", fail_preparation)
    actions = proposal(run)
    with pytest.raises(ValueError, match="未知"):
        _ = apply(run, actions)
    task_id = original_task(run)
    monkeypatch.setattr(registry, "prepare_task_dir", prepare)
    _ = continuation_clock.control(run.scope.project, run.scope.goal_id, "stop")
    with pytest.raises(ValueError, match=r"STOP|授权"):
        _ = apply(run, actions)
    assert not (Path(run.scope.project) / ".model-desk/runs" / task_id / "task.json").exists()


def test_authority_refusal_after_admission_is_not_mislabeled_as_no_effect(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    authorize = governance_context.launch_authority

    @contextmanager
    def denied(state: Record) -> Generator[None]:
        with authorize(state):
            if state.get("automatic_goal"):
                message = "Fixture authority changed after durable stage admission"
                raise governance_context.DispatchBlocked(message)
            yield

    monkeypatch.setattr(governance_context, "launch_authority", denied)
    with pytest.raises(ValueError, match="未知"):
        _ = apply(run, proposal(run))
    assert original_task(run)
