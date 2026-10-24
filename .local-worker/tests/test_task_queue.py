"""The actual shared executor requires durable admission before any worker process."""

from __future__ import annotations

import sys
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, TypedDict

from model_desk_control.database import text_cursor

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import model_registry as registry
import panel_server as panel
import stage_store
from test_goal_action_receipts import GoalRun, apply, proposal
from test_goal_action_receipts import run as run

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest
    from goal_brain_contract import Record
    from pydantic import JsonValue


class StartupEntry(TypedDict):
    run: Path
    project: str


class PanelControls(Protocol):
    def recover_pending_tasks(self, entries: list[StartupEntry]) -> list[Record]: ...


def controls() -> PanelControls:
    return panel


def capture(monkeypatch: pytest.MonkeyPatch) -> list[Callable[[], None]]:
    callbacks: list[Callable[[], None]] = []

    def submit(callback: Callable[[str], None], task_id: str) -> None:
        callbacks.append(partial(callback, task_id))

    monkeypatch.setattr(panel.EXECUTOR, "submit", submit)
    return callbacks


def process(monkeypatch: pytest.MonkeyPatch, run: GoalRun, task_id: str) -> list[str]:
    launches: list[str] = []

    class Worker:
        pid: int = 900003

        def wait(self) -> int:
            _ = registry.patch_task(registry.task_dir(task_id), status="completed")
            return 0

    def launch(*_args: JsonValue, **_kwargs: JsonValue) -> Worker:
        launches.append(run.scope.project)
        return Worker()

    monkeypatch.setattr("subprocess.Popen", launch)
    return launches


def test_task_has_a_durable_queue_receipt_before_callback_execution(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    callbacks = capture(monkeypatch)
    task_id = apply(run, proposal(run)).dispatched_task_ids[0]
    assert len(callbacks) == 1
    with stage_store.connection(run.scope.project) as database:
        receipt = text_cursor(
            database, "SELECT data FROM goal_task_queue WHERE task_id=?", (task_id,)
        ).fetchone()
    assert receipt is not None


def test_duplicate_delivery_starts_the_worker_only_once(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    callbacks = capture(monkeypatch)
    task_id = apply(run, proposal(run)).dispatched_task_ids[0]
    launches = process(monkeypatch, run, task_id)
    callbacks[0]()
    callbacks[0]()
    assert len(launches) == 1


def test_restart_does_not_require_human_confirmation_for_proven_unstarted_queue(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = capture(monkeypatch)
    task_id = apply(run, proposal(run)).dispatched_task_ids[0]
    directory = registry.task_dir(task_id)
    # This is the actual startup reconciler; its entry path is intentionally a Path.
    _ = controls().recover_pending_tasks([{"run": directory, "project": run.scope.project}])
    assert registry.read_task(directory)["status"] == "queued"


def test_failed_submission_fences_a_callback_even_if_it_was_received(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    callbacks: list[Callable[[], None]] = []
    tasks: list[str] = []

    def submit(callback: Callable[[str], None], task_id: str) -> None:
        callbacks.append(partial(callback, task_id))
        tasks.append(task_id)
        reason = "Fixture submission response lost"
        raise RuntimeError(reason)

    monkeypatch.setattr(panel.EXECUTOR, "submit", submit)
    _ = apply(run, proposal(run))
    assert len(tasks) == 1
    launches = process(monkeypatch, run, tasks[0])
    callbacks[0]()
    assert launches == []
