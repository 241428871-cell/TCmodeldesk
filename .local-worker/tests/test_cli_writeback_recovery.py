"""Original task recovery uses saved files and never invokes the model again."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Literal, Protocol, assert_never

import pytest
from model_desk_control import file_writeback
from model_desk_control.host_queue import advance
from model_desk_control.host_queue_models import Phase, Process, QueueState
from model_desk_control.host_queue_store import write
from pydantic import JsonValue

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cli_worker
import cli_writeback
import continuation_clock
import continuation_store
import execution_leases
import goal_requirements
import model_registry as registry
import panel_server as panel
import stage_store
import task_queue
from test_goal_action_receipts import GoalRun, apply, proposal
from test_goal_action_receipts import run as run  # noqa: PLC0414 - imported pytest fixture

type Record = dict[str, JsonValue]
CAPTURE = "fixture-capture"

KILL_DURING_WRITEBACK = r"""
import os
import signal
import subprocess
import sys
from pathlib import Path

worker = Path(sys.argv[1])
sys.path.insert(0, str(worker.parent / "control-plane" / "src"))
sys.path.insert(0, str(worker))
import model_registry as registry
registry.HERE = Path(sys.argv[2])
import panel_server as panel

original_patch = registry.patch_task
ready = Path(sys.argv[4])

def stop_after_first_file(path, **fields):
    if fields.get("writeback_recovered_files") == ["a.txt"]:
        ready.write_text("first replacement durable", encoding="utf-8")
        signal.pause()
    return original_patch(path, **fields)

def forbidden(*_args, **_kwargs):
    os._exit(97)

registry.patch_task = stop_after_first_file
panel.EXECUTOR.submit = forbidden
subprocess.Popen = forbidden
panel.recover_task(sys.argv[3], {"confirm": True})
os._exit(98)
"""


class RecoveryPanel(Protocol):
    def recover_task(self, task_id: str, body: Record) -> Record: ...


def recovery_panel() -> RecoveryPanel:
    return panel


@pytest.fixture
def saved(run: GoalRun, monkeypatch: pytest.MonkeyPatch) -> Path:
    task_id = apply(run, proposal(run, edit=True)).dispatched_task_ids[0]
    directory = registry.task_dir(task_id)
    state = registry.read_task(directory)
    queued = task_queue.observed(state)
    assert queued is not None
    with continuation_store.mutation(run.scope.project, run.scope.goal_id) as database:
        for phase in (Phase.CLAIMED, Phase.ARMED, Phase.STARTED):
            assert advance(database, task_id, queued.delivery_id, phase,
                           launcher=Process(pid=2147483647, birth=1.0)) is not None
    source = Path(run.scope.project) / "fixture-output"
    source.mkdir()
    _ = (source / "generated.txt").write_text("saved output")
    baseline = file_writeback.snapshot(Path(run.scope.project), ("generated.txt",))
    capture = directory / "native-cli" / CAPTURE
    plan = file_writeback.preserve(source, capture / "proposed-files", baseline)
    cli_writeback.prepare(capture, state, plan, answer="native finished", test=None)
    _ = registry.patch_task(directory, status="recovery_pending", cost_unknown_calls=1)

    def forbidden(*_args: JsonValue, **_kwargs: JsonValue) -> None:
        pytest.fail("recovery must not launch a process or enqueue model work")

    monkeypatch.setattr(panel.EXECUTOR, "submit", forbidden)
    monkeypatch.setattr("subprocess.Popen", forbidden)
    return directory


def test_recovers_saved_result_and_original_queue_without_model(saved: Path) -> None:
    state = registry.read_task(saved)
    result = recovery_panel().recover_task(saved.name, {"confirm": True})
    assert result["status"] == "completed"
    assert (Path(str(state["project"])) / "generated.txt").read_text() == "saved output"
    finished = registry.read_task(saved)
    assert finished["answer"] == "native finished"
    assert finished["cost_unknown_calls"] == 1
    assert finished["changed_selected_files"] == ["generated.txt"]
    receipt = task_queue.observed(finished)
    assert receipt is not None
    assert receipt.phase == Phase.DONE
    assert execution_leases.state().held == ()


def test_crash_after_replace_before_receipt_is_reconciled(saved: Path) -> None:
    state = registry.read_task(saved)
    target = Path(str(state["project"])) / "generated.txt"
    _ = target.write_text("saved output")
    before = target.stat().st_mtime_ns
    _ = recovery_panel().recover_task(saved.name, {"confirm": True})
    assert target.stat().st_mtime_ns == before
    assert registry.read_task(saved)["changed_selected_files"] == ["generated.txt"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-kill recovery acceptance")
def test_process_kill_during_multi_file_recovery_resumes_without_model(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    actions = proposal(run, edit=True)
    actions["plan"]["stages"][0]["files"] = ["a.txt", "b.txt"]
    task_id = apply(run, actions).dispatched_task_ids[0]
    directory = registry.task_dir(task_id)
    state = registry.read_task(directory)
    queued = task_queue.observed(state)
    assert queued is not None
    with continuation_store.mutation(run.scope.project, run.scope.goal_id) as database:
        for phase in (Phase.CLAIMED, Phase.ARMED, Phase.STARTED):
            assert advance(
                database,
                task_id,
                queued.delivery_id,
                phase,
                launcher=Process(pid=2147483647, birth=1.0),
            ) is not None
    source = Path(run.scope.project) / "multi-file-output"
    source.mkdir()
    _ = (source / "a.txt").write_text("proposal-a")
    _ = (source / "b.txt").write_text("proposal-b")
    capture = directory / "native-cli" / "multi-file-capture"
    plan = file_writeback.preserve(
        source,
        capture / "proposed-files",
        file_writeback.snapshot(Path(run.scope.project), ("a.txt", "b.txt")),
    )
    cli_writeback.prepare(capture, state, plan, answer="multi-file native result", test=None)
    _ = registry.patch_task(directory, status="recovery_pending", cost_unknown_calls=1)
    ready = directory / "kill-after-first-writeback"
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            KILL_DURING_WRITEBACK,
            str(Path(cli_writeback.__file__).parent),
            str(registry.HERE),
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
        pytest.fail(f"controller did not reach the durable writeback boundary: {stderr}")
    process.kill()
    assert process.wait(timeout=5) == -signal.SIGKILL

    project = Path(run.scope.project)
    assert (project / "a.txt").read_text() == "proposal-a"
    assert not (project / "b.txt").exists()
    assert registry.read_task(directory)["status"] == "recovery_pending"

    def forbidden(*_args: JsonValue, **_kwargs: JsonValue) -> None:
        pytest.fail("partial writeback recovery must not launch model work")

    monkeypatch.setattr(panel.EXECUTOR, "submit", forbidden)
    monkeypatch.setattr("subprocess.Popen", forbidden)
    result = recovery_panel().recover_task(task_id, {"confirm": True})
    assert result["status"] == "completed"
    assert (project / "a.txt").read_text() == "proposal-a"
    assert (project / "b.txt").read_text() == "proposal-b"
    finished = registry.read_task(directory)
    assert finished["changed_selected_files"] == ["a.txt", "b.txt"]
    assert finished["cost_unknown_calls"] == 1


@pytest.mark.parametrize("mutation", ["prompt", "files", "plan", "candidate", "test", "capture"])
def test_changed_evidence_never_writes_or_redelivers(
    saved: Path, mutation: Literal["prompt", "files", "plan", "candidate", "test", "capture"]
) -> None:
    match mutation:
        case "prompt":
            _ = (saved / "prompt.txt").write_text("changed input")
        case "files":
            _ = registry.patch_task(saved, files=["other.txt"])
        case "plan":
            _ = (saved / "native-cli" / CAPTURE / "writeback-plan.json").write_text("{}")
        case "candidate":
            candidate = saved / "native-cli" / CAPTURE / "proposed-files/generated.txt"
            _ = candidate.write_text("changed")
        case "test":
            _ = registry.patch_task(saved, test_command="false")
        case "capture":
            _ = registry.patch_task(saved, writeback_capture_id="../foreign")
        case _:
            assert_never(mutation)
    with pytest.raises(ValueError, match=r"writeback|validation"):
        _ = recovery_panel().recover_task(saved.name, {"confirm": True})
    state = registry.read_task(saved)
    assert state["status"] == "recovery_pending"
    assert not (Path(str(state["project"])) / "generated.txt").exists()


def test_conflict_keeps_human_file_and_saved_proposal(saved: Path) -> None:
    state = registry.read_task(saved)
    target = Path(str(state["project"])) / "generated.txt"
    _ = target.write_text("human output")
    with pytest.raises(file_writeback.WritebackConflictError):
        _ = recovery_panel().recover_task(saved.name, {"confirm": True})
    assert target.read_text() == "human output"
    assert (saved / "native-cli" / CAPTURE / "proposed-files/generated.txt").is_file()


def test_stop_and_unconfirmed_worker_block_saved_file_recovery(
    saved: Path, run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unknown(_state: Record) -> str:
        return "unknown"

    def gone(_state: Record) -> bool:
        return False

    monkeypatch.setattr(panel, "recorded_worker_alive", unknown)
    with pytest.raises(ValueError, match="进程状态"):
        _ = recovery_panel().recover_task(saved.name, {"confirm": True})
    monkeypatch.setattr(panel, "recorded_worker_alive", gone)
    _ = continuation_clock.control(run.scope.project, run.scope.goal_id, "stop")
    with pytest.raises(ValueError, match="STOP"):
        _ = recovery_panel().recover_task(saved.name, {"confirm": True})
    assert not (Path(run.scope.project) / "generated.txt").exists()


def test_new_requirement_fences_saved_writeback_from_an_older_brain_round(
    saved: Path, run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = registry.read_task(saved)
    assert state["goal_round_id"] == run.scope.operation_id
    assert state["goal_requirements_revision"] == 0
    _ = goal_requirements.insert(
        run.scope.project,
        run.scope.goal_id,
        "Do not publish the previous generated file until the plan is revised.",
        request_id="writeback-fence",
    )

    def gone(_state: Record) -> bool:
        return False

    monkeypatch.setattr(panel, "recorded_worker_alive", gone)
    with pytest.raises(ValueError, match="运行中需求已经更新"):
        _ = recovery_panel().recover_task(saved.name, {"confirm": True})
    current = registry.read_task(saved)
    assert current["status"] == "failed"
    assert current["writeback_state"] == "superseded"
    assert current["recovery"]["kind"] == "requirements_superseded"
    stage = stage_store.get_stage(str(current["stage_id"]), run.scope.project)
    assert stage["data"]["requirements_superseded"]["task_id"] == saved.name
    assert not (Path(run.scope.project) / "generated.txt").exists()
    assert execution_leases.state().held == ()


def test_missing_requirement_snapshot_never_becomes_a_legacy_writeback(
    saved: Path, run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = registry.read_task(saved)
    task.pop("goal_requirements_revision")
    registry.write_json(saved / "task.json", task)

    def gone(_state: Record) -> bool:
        return False

    monkeypatch.setattr(panel, "recorded_worker_alive", gone)
    with pytest.raises(ValueError, match="缺少可信的运行中需求版本"):
        _ = recovery_panel().recover_task(saved.name, {"confirm": True})
    assert registry.read_task(saved)["status"] == "recovery_pending"
    assert not (Path(run.scope.project) / "generated.txt").exists()
    assert execution_leases.state().held == ()


def test_receipt_commit_gap_can_finish_without_rewriting_files(
    saved: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = registry.patch_task

    def fail_final(path: Path, **fields: JsonValue) -> Record:
        if fields.get("status") == "completed":
            message = "fixture final metadata write failed"
            raise OSError(message)
        return original(path, **fields)

    monkeypatch.setattr(registry, "patch_task", fail_final)
    with pytest.raises(OSError, match="metadata write failed"):
        _ = recovery_panel().recover_task(saved.name, {"confirm": True})
    state = registry.read_task(saved)
    queue = task_queue.observed(state)
    assert queue is not None
    assert queue.phase == Phase.DONE
    target = Path(str(state["project"])) / "generated.txt"
    before = target.stat().st_mtime_ns
    monkeypatch.setattr(registry, "patch_task", original)
    _ = recovery_panel().recover_task(saved.name, {"confirm": True})
    assert target.stat().st_mtime_ns == before
    assert registry.read_task(saved)["status"] == "completed"


def test_missing_completion_receipt_never_falls_back_to_model(saved: Path) -> None:
    (saved / "native-cli" / CAPTURE / "writeback-receipt.json").unlink()
    with pytest.raises(FileNotFoundError):
        _ = recovery_panel().recover_task(saved.name, {"confirm": True})
    assert registry.read_task(saved)["status"] == "recovery_pending"


def test_busy_lease_and_changed_queue_block_before_any_file_write(
    saved: Path, run: GoalRun
) -> None:
    state = registry.read_task(saved)
    request = execution_leases.request_for(state, {"provider": "local", "id": "fixture"})
    assert execution_leases.BROKER.try_acquire(request)
    try:
        with pytest.raises(execution_leases.LeaseDuplicateError):
            _ = recovery_panel().recover_task(saved.name, {"confirm": True})
    finally:
        execution_leases.release(request.owner)
    previous = task_queue.observed(state)
    assert previous is not None
    blocked = QueueState(ticket=previous.ticket, phase=Phase.BLOCKED,
                         delivery_id=previous.delivery_id, owner=previous.owner)
    with continuation_store.mutation(run.scope.project, run.scope.goal_id) as database:
        write(database, blocked, previous)
    with pytest.raises(file_writeback.WritebackError, match="queue_identity_changed"):
        _ = recovery_panel().recover_task(saved.name, {"confirm": True})
    assert not (Path(run.scope.project) / "generated.txt").exists()


def test_real_child_writeback_gap_enters_file_only_recovery(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = apply(run, proposal(run, edit=True)).dispatched_task_ids[0]
    directory = registry.task_dir(task_id)
    state = registry.read_task(directory)
    queued = task_queue.observed(state)
    assert queued is not None
    with continuation_store.mutation(run.scope.project, run.scope.goal_id) as database:
        for phase in (Phase.CLAIMED, Phase.ARMED, Phase.STARTED):
            assert advance(database, task_id, queued.delivery_id, phase,
                           launcher=Process(pid=2147483647, birth=1.0)) is not None

    def command(
        _engine: str, _model: str, _project: Path, _result: Path, _kind: str,
        _runtime: cli_worker.CliRuntime | None = None,
    ) -> list[str]:
        script = "import sys;from pathlib import Path;sys.stdin.read()"
        script += ";Path('generated.txt').write_text('native result');print('done')"
        return [sys.executable, "-c", script]

    original = registry.patch_task

    def interrupt_receipt(path: Path, **fields: JsonValue) -> Record:
        if "changed_selected_files" in fields:
            message = "fixture receipt write interrupted"
            raise OSError(message)
        return original(path, **fields)

    monkeypatch.setattr(cli_worker, "command", command)
    monkeypatch.setattr(registry, "patch_task", interrupt_receipt)
    monkeypatch.setattr(sys, "argv", ["cli_worker", "--run-id", task_id])
    assert cli_worker.main() == 1
    pending = registry.read_task(directory)
    assert pending["status"] == "recovery_pending"
    assert (Path(run.scope.project) / "generated.txt").read_text() == "native result"
    monkeypatch.setattr(registry, "patch_task", original)

    def gone(_state: Record) -> bool:
        return False

    def forbidden(*_args: JsonValue, **_kwargs: JsonValue) -> None:
        pytest.fail("native call must not be repeated during file recovery")

    monkeypatch.setattr(panel, "recorded_worker_alive", gone)
    monkeypatch.setattr("subprocess.Popen", forbidden)
    result = recovery_panel().recover_task(task_id, {"confirm": True})
    assert result["status"] == "completed"
    assert registry.read_task(directory)["cost_unknown_calls"] == 1


def test_requirement_inserted_after_native_result_prevents_original_project_writeback(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = apply(run, proposal(run, edit=True)).dispatched_task_ids[0]
    directory = registry.task_dir(task_id)
    state = registry.read_task(directory)
    queued = task_queue.observed(state)
    assert queued is not None
    with continuation_store.mutation(run.scope.project, run.scope.goal_id) as database:
        for phase in (Phase.CLAIMED, Phase.ARMED, Phase.STARTED):
            assert advance(
                database,
                task_id,
                queued.delivery_id,
                phase,
                launcher=Process(pid=2147483647, birth=1.0),
            ) is not None

    def command(
        _engine: str, _model: str, _project: Path, _result: Path, _kind: str,
        _runtime: cli_worker.CliRuntime | None = None,
    ) -> list[str]:
        script = "import sys;from pathlib import Path;sys.stdin.read()"
        script += ";Path('generated.txt').write_text('stale native result');print('done')"
        return [sys.executable, "-c", script]

    original_prepare = cli_writeback.prepare

    def insert_requirement_after_capture(
        capture: Path,
        saved_state: Record,
        plan: file_writeback.WritebackPlan,
        *,
        answer: str,
        test: Record | None,
    ) -> None:
        original_prepare(capture, saved_state, plan, answer=answer, test=test)
        _ = goal_requirements.insert(
            run.scope.project,
            run.scope.goal_id,
            "The generated file must now use the revised content.",
            request_id="native-writeback-fence",
        )

    monkeypatch.setattr(cli_worker, "command", command)
    monkeypatch.setattr(cli_writeback, "prepare", insert_requirement_after_capture)
    monkeypatch.setattr(sys, "argv", ["cli_worker", "--run-id", task_id])

    assert cli_worker.main() == 1
    finished = registry.read_task(directory)
    assert finished["status"] == "failed"
    assert finished["writeback_state"] == "superseded"
    assert finished["recovery"]["kind"] == "requirements_superseded"
    stage = stage_store.get_stage(str(finished["stage_id"]), run.scope.project)
    assert stage["data"]["requirements_superseded"]["task_id"] == task_id
    assert not (Path(run.scope.project) / "generated.txt").exists()
    candidate = directory / "native-cli" / str(finished["writeback_capture_id"])
    assert (candidate / "proposed-files/generated.txt").read_text() == "stale native result"
