"""Permanent deletion covers every new durable record and keeps live work protected."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING
from unittest import mock

import pytest
from model_desk_control.database import text_cursor

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import continuation_clock
import goal_brain_runner
import goal_controller
import model_registry as registry
import panel_server as panel
import stage_store
import task_graph_bridge
import task_history as history
from test_goal_action_receipts import run as run  # noqa: PLC0414 - imported pytest fixture
from test_task_branches import answer, parked

if TYPE_CHECKING:
    from goal_brain_contract import Record
    from test_goal_action_receipts import GoalRun

MARKER = "PRIVATE-PROMPT-7f3a"
KILL_AFTER_PURGE_COMMIT = r"""
import signal
import sys
from pathlib import Path

worker = Path(sys.argv[1])
sys.path.insert(0, str(worker.parent / "control-plane" / "src"))
sys.path.insert(0, str(worker))
import model_registry as registry
registry.HERE = Path(sys.argv[2])
import task_history as history

ready = Path(sys.argv[5])

def stop_after_commit(_job_id, _manifest, _directory):
    ready.write_text("deletion journal committed", encoding="utf-8")
    signal.pause()

history._complete = stop_after_commit
selected = history.preview(
    {"mode": "project", "project": sys.argv[3]},
    [sys.argv[4]],
)
history.delete(
    {"mode": "project", "project": sys.argv[3]},
    selected["token"],
)
raise SystemExit(98)
"""
HOST_TABLES = (
    "continuation_intents",
    "continuation_clocks",
    "goal_graph_steps",
    "goal_events",
    "goal_task_queue",
    "goal_task_initializations",
    "goal_task_branches",
    "human_decisions",
    "human_decision_uses",
    "plan_revisions",
    "goal_contracts",
)


def idle() -> Record:
    return {
        "plan": None,
        "questions": [],
        "dispatch_stage_ids": [],
        "reviews": [],
        "obligations": [],
        "complete": False,
        "summary": "Observe",
    }


def view(run: GoalRun) -> dict[str, str]:
    return {"mode": "project", "project": run.scope.project}


def records_bytes(run: GoalRun) -> bytes:
    desk = Path(run.scope.project) / ".model-desk"
    return b"".join(
        path.read_bytes() for path in desk.glob("records.db*") if path.is_file()
    )


def count(run: GoalRun, table: str) -> int:
    with stage_store.connection(run.scope.project) as database:
        present = text_cursor(
            database, "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchall()
        if not present:
            return 0
        statement = "SELECT CAST(COUNT(*) AS TEXT) FROM " + table
        return int(text_cursor(database, statement).fetchall()[0][0])


def threads(run: GoalRun, name: str) -> int:
    path = Path(run.scope.project) / ".model-desk" / "langgraph" / name
    if not path.is_file():
        return 0
    with sqlite3.connect(path) as database:
        rows = text_cursor(database, "SELECT CAST(COUNT(*) AS TEXT) FROM checkpoints").fetchall()
        return int(rows[0][0])


def finished_goal(run: GoalRun, monkeypatch: pytest.MonkeyPatch) -> str:
    """A real round history: a parked branch, a denied card, a goal-graph round, a done task."""
    _ = parked(run)
    answer(run, 1)

    def native(_context: Record, *_args: str, **_kwargs: str) -> Record:
        return {
            "actions": idle(),
            "native_session_id": "purge-session",
            "receipt": run.scope.project + "/purge-result.json",
        }

    monkeypatch.setattr(goal_brain_runner, "run", native)
    assert goal_controller.run_once(run.scope.project, run.scope.goal_id, panel) is not None
    outputs = Path(run.scope.project) / ".model-desk" / "goal-brain" / run.scope.goal_id / "round"
    outputs.mkdir(parents=True)
    _ = (outputs / "native-output.txt").write_text(MARKER, encoding="utf-8")
    workspace = outputs.parent / "native-workspace"
    workspace.mkdir()
    _ = (workspace / "source-copy.py").write_text(MARKER, encoding="utf-8")
    _ = (outputs.parent / "native-workspace-session.json").write_text(
        '{"schema":1,"session_id":"purge-session"}', encoding="utf-8"
    )
    task_id = run.queued[0]
    _ = registry.patch_task(registry.task_dir(task_id), status="completed", ended_at=registry.now())
    with stage_store.connection(run.scope.project) as database:
        # The mocked executor never ran; emulate its finished queue and accepted stages.
        _ = database.execute("UPDATE goal_task_queue SET phase='done'")
        _ = database.execute(
            "UPDATE stages SET status='accepted' WHERE goal_id=?", (run.scope.goal_id,)
        )
    _ = stage_store.set_goal_status(run.scope.goal_id, "cancelled", project=run.scope.project)
    return task_id


def with_marker(run: GoalRun) -> None:
    with stage_store.connection(run.scope.project) as database:
        _ = database.execute(
            "UPDATE goal_task_initializations SET data=json_set(data,'$.marker',?)", (MARKER,)
        )
        _ = database.execute("UPDATE goals SET objective=? WHERE id=?", (MARKER, run.scope.goal_id))


def test_deletion_removes_every_goal_record_checkpoint_and_private_copy(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = finished_goal(run, monkeypatch)
    with_marker(run)
    assert MARKER.encode() in records_bytes(run)
    assert threads(run, "checkpoints.sqlite") > 0
    assert threads(run, "task-checkpoints.sqlite") > 0
    assert all(count(run, table) for table in ("goal_task_initializations", "goal_task_branches"))
    preview = history.preview(view(run), [task_id])
    assert preview["records"]["goal_task_initializations"] >= 1
    assert preview["goal_brain_rounds"] == 1
    assert MARKER not in json.dumps(preview, ensure_ascii=False)
    result = history.delete(view(run), preview["token"])
    assert not result["cleanup_pending"]
    for table in HOST_TABLES:
        assert count(run, table) == 0, table
    assert not (Path(run.scope.project) / ".model-desk" / "goal-brain" / run.scope.goal_id).exists()
    assert not registry.task_dir(task_id).exists()
    assert threads(run, "checkpoints.sqlite") == 0
    assert threads(run, "task-checkpoints.sqlite") == 0
    assert MARKER.encode() not in records_bytes(run)
    assert list(history.staging_root().iterdir()) == []


def test_active_round_or_open_queue_keeps_the_whole_group(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = finished_goal(run, monkeypatch)
    with stage_store.connection(run.scope.project) as database:
        _ = database.execute("UPDATE goal_task_queue SET phase='armed'")
    with pytest.raises(ValueError, match="执行队列仍未结束"):
        _ = history.preview(view(run), [task_id])
    with stage_store.connection(run.scope.project) as database:
        _ = database.execute("UPDATE goal_task_queue SET phase='done'")
        _ = database.execute(
            "UPDATE continuation_intents SET status='unknown' WHERE id=?", (run.scope.operation_id,)
        )
    with pytest.raises(ValueError, match="续跑轮次仍在进行或待核对"):
        _ = history.preview(view(run), [task_id])
    assert registry.task_dir(task_id).exists()


def test_unsettled_fee_or_unknown_call_blocks_deletion(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = finished_goal(run, monkeypatch)
    _ = registry.patch_task(
        registry.task_dir(task_id), cost_settled_cny=0.0, cost_unsettled_cny=0.25
    )
    with pytest.raises(ValueError, match="费用尚未结算"):
        _ = history.preview(view(run), [task_id])
    _ = registry.patch_task(
        registry.task_dir(task_id), cost_unsettled_cny=0.0, cost_unknown_calls=1
    )
    with pytest.raises(ValueError, match="费用尚未结算"):
        _ = history.preview(view(run), [task_id])


def test_pending_obligation_blocks_deletion(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = finished_goal(run, monkeypatch)
    with stage_store.connection(run.scope.project) as database:
        _ = database.execute(
            "UPDATE continuation_clocks SET data=json_set(data,'$.obligations',json(?))",
            (json.dumps([{"id": "o1", "title": "Archive", "status": "pending"}]),),
        )
    with pytest.raises(ValueError, match="收尾义务"):
        _ = history.preview(view(run), [task_id])


def test_failure_before_commit_restores_runs_rounds_and_rows(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = finished_goal(run, monkeypatch)
    before = {table: count(run, table) for table in HOST_TABLES}
    preview = history.preview(view(run), [task_id])
    rename = os.rename

    def fail_rounds(source: str | Path, destination: str | Path) -> None:
        if Path(source).name == run.scope.goal_id:
            reason = "Fixture locked the round directory"
            raise PermissionError(reason)
        rename(source, destination)

    with (
        mock.patch.object(history.os, "rename", side_effect=fail_rounds),
        pytest.raises(PermissionError, match="Fixture locked"),
    ):
        _ = history.delete(view(run), preview["token"])
    assert registry.task_dir(task_id).exists()
    assert (Path(run.scope.project) / ".model-desk" / "goal-brain" / run.scope.goal_id).exists()
    assert {table: count(run, table) for table in HOST_TABLES} == before
    assert list(history.staging_root().iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-kill recovery acceptance")
def test_committed_job_is_completed_by_new_process_after_sigkill(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = finished_goal(run, monkeypatch)
    ready = Path(run.scope.project) / "purge-commit-ready"
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            KILL_AFTER_PURGE_COMMIT,
            str(Path(history.__file__).parent),
            str(registry.HERE),
            run.scope.project,
            task_id,
            str(ready),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 10
    while not ready.is_file() and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    if not ready.is_file():
        stderr = process.communicate(timeout=5)[1]
        pytest.fail(f"deletion process did not reach its commit point: {stderr}")
    process.kill()
    assert process.wait(timeout=5) == -signal.SIGKILL

    assert count(run, "goal_task_initializations") == 0
    assert threads(run, "task-checkpoints.sqlite") > 0
    history.recover()
    assert threads(run, "checkpoints.sqlite") == 0
    assert threads(run, "task-checkpoints.sqlite") == 0
    assert list(history.staging_root().iterdir()) == []
    with stage_store.connection(run.scope.project) as database:
        phases = text_cursor(database, "SELECT phase FROM task_deletion_log").fetchall()
    assert [tuple(row) for row in phases] == [("purged",)]


def test_unrelated_goal_and_its_checkpoints_survive(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = finished_goal(run, monkeypatch)
    other = stage_store.create_goal("Other", "Keep this goal", run.scope.project, store="project")
    _ = continuation_clock.configure_clock(run.scope.project, other["id"], interval_seconds=1800)
    path = task_graph_bridge.checkpoint_path(run.scope.project)
    with sqlite3.connect(path) as database:
        _ = text_cursor(
            database,
            "INSERT INTO checkpoints VALUES (?,?,?,?,?,?,?)",
            ("native-task-v1:keep", "", "1", "", "json", "{}", "{}"),
        )
    preview = history.preview(view(run), [task_id])
    _ = history.delete(view(run), preview["token"])
    assert stage_store.get_goal(other["id"], run.scope.project) is not None
    with stage_store.connection(run.scope.project) as database:
        kept = text_cursor(
            database, "SELECT goal_id FROM continuation_clocks WHERE goal_id=?", (other["id"],)
        ).fetchall()
    assert len(kept) == 1
    assert threads(run, "task-checkpoints.sqlite") == 1
