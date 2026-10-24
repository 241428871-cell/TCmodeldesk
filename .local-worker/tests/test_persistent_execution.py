"""Drive the production executor through real local processes, never a model CLI."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import psutil
import pytest

from model_desk_control.durable_lease_models import LeasePhase

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import execution_leases
import goal_interactions
import lease_runtime
import model_registry as registry
import panel_server as panel
import task_queue
from test_cli_writeback_recovery import recovery_panel
from test_goal_action_receipts import GoalRun, apply, proposal
from test_goal_action_receipts import run as run
from test_task_queue import capture

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import IO

    from pydantic import JsonValue


def worker(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch, body: str
) -> tuple[Path, Callable[[], None]]:
    callbacks = capture(monkeypatch)
    task_id = apply(run, proposal(run, edit=True)).dispatched_task_ids[0]
    directory = registry.task_dir(task_id)
    scripts = Path(run.scope.project) / "worker-fixture"
    scripts.mkdir()
    _ = (scripts / "cli_worker.py").write_text(
        f"""import os, sys
from pathlib import Path
sys.path.insert(0, {str(Path(execution_leases.__file__).parent)!r})
import model_registry as registry
import lease_runtime
from model_desk_control.resource_leases import LeaseRequest
directory = Path({str(directory)!r})
state = registry.read_task(directory)
lease_runtime.verify(state['execution_lease_token'],
    LeaseRequest.model_validate_json(state['execution_lease']))
"""
        + body,
        encoding="utf-8",
    )
    monkeypatch.setattr(panel, "HERE", scripts)
    monkeypatch.setattr(panel, "PYTHON", Path(sys.executable))
    return directory, callbacks[0]


@pytest.mark.skipif(os.name == "nt", reason="POSIX session evidence; Windows Job Object pending")
def test_real_executor_registers_worker_before_effects_and_releases_after_exit(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given an original governed task whose worker only writes an isolated marker.
    directory, execute = worker(
        run,
        monkeypatch,
        """(directory / 'verified').write_text('yes')
registry.patch_task(directory, status='completed')
""",
    )
    # When the real parent launches the normal bootstrap and waits for it.
    execute()
    # Then the child proved durable ownership and its drained session is released.
    state = registry.read_task(directory)
    assert (directory / "verified").read_text() == "yes"
    assert state["status"] == "completed"
    receipt = lease_runtime.current(str(state["execution_lease_token"]))
    assert receipt.phase == LeasePhase.RELEASED
    assert receipt.launcher is not None
    assert execution_leases.state().held == ()


@pytest.mark.skipif(os.name == "nt", reason="POSIX reparenting/session evidence")
@pytest.mark.parametrize("detached", [False, True])
def test_live_reparented_child_keeps_lease_and_prevents_recovery_takeover(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch, *, detached: bool
) -> None:
    # Given a worker that completes while a real background child still owns its session.
    directory, execute = worker(
        run,
        monkeypatch,
        f"""import subprocess
child = subprocess.Popen([sys.executable, '-c', 'import signal; signal.pause()'],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    start_new_session={detached!r})
(directory / 'child.pid').write_text(str(child.pid))
registry.patch_task(directory, status='completed')
""",
    )
    execute()
    child = psutil.Process(int((directory / "child.pid").read_text()))
    try:
        state = registry.read_task(directory)
        token = str(state["execution_lease_token"])
        receipt = lease_runtime.current(token)
        # When recovery attempts to take over despite the root worker having exited.
        with pytest.raises(ValueError, match="lease_recovery_not_quiescent"):
            _ = lease_runtime.takeover(receipt.request, token)
        # Then the original task is pending and its original generation stays held.
        assert state["status"] == "recovery_pending"
        assert lease_runtime.current(token) == receipt
        assert directory.name in execution_leases.state().held
    finally:
        child.kill()
        try:
            _ = child.wait(timeout=5)
        except psutil.TimeoutExpired:
            assert child.status() == psutil.STATUS_ZOMBIE
    replacement = lease_runtime.takeover(receipt.request, token)
    assert replacement.token != token
    assert lease_runtime.release(replacement.token)


@pytest.mark.skipif(os.name == "nt", reason="POSIX inherited session cancellation")
def test_managed_child_stop_keeps_worker_alive_to_save_receipt(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a nested CLI substitute using the production process options.
    directory, execute = worker(
        run,
        monkeypatch,
        """import subprocess, platform_runtime
child = subprocess.Popen([sys.executable, '-c', 'import signal; signal.pause()'],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    **platform_runtime.process_options())
try:
    platform_runtime.stop_tree(child.pid)
    child.wait(timeout=5)
finally:
    if child.poll() is None:
        child.kill()
        child.wait(timeout=5)
(directory / 'survived').write_text('yes')
registry.patch_task(directory, status='cancelled')
""",
    )
    # When the worker stops the nested process in its shared session.
    execute()
    # Then it survives to save its result and all descendant resources are released.
    state = registry.read_task(directory)
    assert (directory / "survived").read_text() == "yes"
    assert state["status"] == "cancelled"
    assert lease_runtime.current(str(state["execution_lease_token"])).phase == LeasePhase.RELEASED


@pytest.mark.skipif(os.name == "nt", reason="POSIX direction-change process proof")
def test_direction_change_stops_real_managed_child_before_replanning(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory, execute = worker(
        run,
        monkeypatch,
        """import subprocess, platform_runtime, time
registry.patch_task(directory, status='running')
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    **platform_runtime.process_options())
(directory / 'child.pid').write_text(str(child.pid))
while not (directory / 'cancelled.flag').exists():
    time.sleep(.05)
platform_runtime.stop_tree(child.pid)
child.wait(timeout=5)
registry.patch_task(directory, status='cancelled', phase='方向调整已停止旧执行')
""",
    )
    thread = threading.Thread(target=execute, daemon=True)
    thread.start()
    deadline = time.monotonic() + 8
    while not (directory / "child.pid").is_file() and time.monotonic() < deadline:
        time.sleep(.05)
    assert (directory / "child.pid").is_file()
    child_pid = int((directory / "child.pid").read_text())
    assert psutil.pid_exists(child_pid)

    inserted = goal_interactions.interact(
        run.scope.project,
        run.scope.goal_id,
        "Stop the old execution and replace the active direction",
        kind="direction_change",
        request_id="real-child-interrupt",
    )
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert inserted["requirement"] is not None
    assert inserted["requirement"]["interrupted_task_ids"] == [directory.name]
    state = registry.read_task(directory)
    assert state["status"] == "cancelled"
    assert not psutil.pid_exists(child_pid)
    receipt = lease_runtime.current(str(state["execution_lease_token"]))
    assert receipt.phase == LeasePhase.RELEASED


@pytest.mark.skipif(os.name == "nt", reason="POSIX session recovery proof")
@pytest.mark.parametrize("ack_failure", [False, True])
def test_real_executor_saved_writeback_is_recovered_without_second_spawn(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch, *, ack_failure: bool
) -> None:
    # Given a successful native substitute leaves its original saved proposal for recovery.
    directory, execute = worker(
        run,
        monkeypatch,
        """import cli_writeback
from model_desk_control import file_writeback
source = directory / 'source'; source.mkdir()
(source / 'generated.txt').write_text('recovered output')
baseline = file_writeback.snapshot(Path(state['project']), ('generated.txt',))
capture = directory / 'native-cli' / 'integration'
plan = file_writeback.preserve(source, capture / 'proposed-files', baseline)
cli_writeback.prepare(capture, state, plan, answer='done', test=None)
registry.patch_task(directory, status='recovery_pending')
""",
    )
    launched: list[subprocess.Popen[bytes]] = []
    original_spawn = subprocess.Popen

    def spawn(
        command: list[str], *, stdout: IO[str], stderr: int, stdin: int, start_new_session: bool
    ) -> subprocess.Popen[bytes]:
        process = original_spawn(
            command, stdout=stdout, stderr=stderr, stdin=stdin, start_new_session=start_new_session
        )
        launched.append(process)
        return process

    def lost_ack(*_args: JsonValue) -> None:
        reason = "injected parent acknowledgement failure"
        raise OSError(reason)

    monkeypatch.setattr(subprocess, "Popen", spawn)
    if ack_failure:
        monkeypatch.setattr(task_queue, "started", lost_ack)
    execute()
    for process in launched:
        assert process.wait(timeout=10) == 0
    before = registry.read_task(directory)
    token = str(before["execution_lease_token"])
    old = lease_runtime.current(token)
    assert old.phase in (LeasePhase.STARTED, LeasePhase.RELEASED)

    def no_spawn(*_args: str, **_kwargs: str) -> None:
        pytest.fail("file-only recovery must not launch a process")

    monkeypatch.setattr("subprocess.Popen", no_spawn)
    # When the existing recovery endpoint performs an atomic resource takeover.
    result = recovery_panel().recover_task(directory.name, {"confirm": True})
    # Then only the saved file is written under a new, subsequently released generation.
    after = registry.read_task(directory)
    assert result["status"] == "completed"
    assert (Path(run.scope.project) / "generated.txt").read_text() == "recovered output"
    assert after["execution_lease_token"] != token
    assert lease_runtime.current(str(after["execution_lease_token"])).phase == LeasePhase.RELEASED
