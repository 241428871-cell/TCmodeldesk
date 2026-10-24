"""Original cancellation reaches detached descendants without trusting bare PIDs."""

import os
import sys
from pathlib import Path

import psutil
import pytest

from model_desk_control.host_queue_models import Process

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lease_descendants
import lease_stop
import lease_storage
import model_registry as registry
import panel_server as panel
from test_cli_writeback_recovery import recovery_panel
from test_goal_action_receipts import GoalRun
from test_goal_action_receipts import run as run
from test_lease_descendants import started
from test_lease_runtime import home as home
from test_persistent_execution import worker


@pytest.mark.skipif(os.name == "nt", reason="POSIX detached child cancellation")
def test_original_cancel_stops_detached_child_and_keeps_saved_task_cancelled(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory, execute = worker(run, monkeypatch, """import subprocess
child = subprocess.Popen([sys.executable, '-c', 'import signal; signal.pause()'],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    start_new_session=True)
(directory / 'child.pid').write_text(str(child.pid))
registry.patch_task(directory, status='completed')
""")
    execute()
    child = psutil.Process(int((directory / "child.pid").read_text()))
    try:
        assert registry.read_task(directory)["status"] == "recovery_pending"
        _ = panel.cancel_task(directory.name, reason="stop retained descendant")
        _ = child.wait(timeout=5)
        result = panel.cancel_task(directory.name, reason="confirm cleanup")
        stopped = result["descendant_stop"]
        assert isinstance(stopped, dict)
        assert stopped["state"] == "released"
        assert registry.read_task(directory)["status"] == "cancelled"
        assert (directory / "cancelled.flag").exists()
        with pytest.raises(ValueError, match=r"取消|停止|恢复"):
            _ = recovery_panel().recover_task(directory.name, {"confirm": True})
    finally:
        if child.is_running() and child.status() != psutil.STATUS_ZOMBIE:
            child.kill()
            _ = child.wait(timeout=5)


def test_reused_pid_is_never_signalled(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    receipt = started(home)
    assert receipt.launcher is not None
    observed = lease_descendants.Observation(
        token=receipt.token, launcher=receipt.launcher, state="closed",
        descendants=(Process(pid=313, birth=1.0),),
        suspected_orphans=(Process(pid=314, birth=1.0),),
    )
    with lease_storage.transaction() as database:
        _ = database.execute("UPDATE lease_descendants SET data=? WHERE token=?",
                             (observed.model_dump_json(), receipt.token))

    class Replacement:
        def create_time(self) -> float:
            return 2.0

        def status(self) -> str:
            return psutil.STATUS_RUNNING

        def kill(self) -> None:
            pytest.fail("replacement process must not be signalled")

    def process(_pid: int) -> Replacement:
        return Replacement()

    monkeypatch.setattr(psutil, "Process", process)
    result = lease_stop.stop_recorded({
        "id": receipt.request.owner, "project": receipt.request.project,
        "execution_lease_token": receipt.token, "execution_lease_protocol": 1,
    })
    assert result["unconfirmed_count"] == 0
    assert result["suspected_orphan_count"] == 1


def test_foreign_task_receipt_is_not_used_for_signals(home: Path) -> None:
    receipt = started(home)
    result = lease_stop.stop_recorded({
        "id": "another-task", "project": receipt.request.project,
        "execution_lease_token": receipt.token, "execution_lease_protocol": 1,
    })
    assert result == {"state": "unknown", "note": "租约不属于当前任务，未操作其进程"}
