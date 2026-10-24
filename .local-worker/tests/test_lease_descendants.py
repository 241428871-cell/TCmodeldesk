"""Unknown or incomplete descendant observations cannot release resources."""

import json
import subprocess
import sys
from pathlib import Path

import psutil
import pytest
from model_desk_control import durable_leases as leases
from model_desk_control.durable_lease_models import DurableLease
from model_desk_control.host_queue_models import Process

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lease_descendants
import lease_processes
import lease_runtime
import lease_storage
from test_lease_runtime import home as home  # noqa: PLC0414 - pytest fixture re-export.
from test_lease_runtime import request


def started(home: Path) -> DurableLease:
    receipt = lease_runtime.reserve(request(home))
    assert receipt is not None
    lease_runtime.arm(receipt.token)
    with lease_storage.transaction() as database:
        receipt = leases.started(database, lease_runtime.current(receipt.token),
                                 Process(pid=2147483647, birth=1.0), containment="posix_session")
        lease_descendants.initialize(database, receipt.token, Process(pid=2147483647, birth=1.0))
    return receipt


def test_incomplete_observation_is_not_exit_proof(home: Path) -> None:
    receipt = started(home)
    assert not lease_descendants.drained(receipt, lease_processes.exited)
    assert not lease_runtime.release(receipt.token)


def test_access_denied_is_persisted_as_unknown(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    receipt = started(home)

    def denied(_previous: lease_descendants.Observation) -> tuple[Process, ...]:
        raise psutil.AccessDenied(pid=2147483647)

    monkeypatch.setattr(lease_descendants, "_scan", denied)
    lease_descendants.sample(home, receipt, final=True)
    with lease_storage.readonly() as database:
        assert database is not None
        observed = lease_descendants.read(database, receipt)
    assert observed is not None
    assert observed.state == "unknown"
    assert not lease_descendants.drained(receipt, lease_processes.exited)


def test_old_receipts_do_not_invent_descendant_coverage(home: Path) -> None:
    receipt = started(home)
    with lease_storage.transaction() as database:
        _ = database.execute("DELETE FROM lease_descendants WHERE token=?", (receipt.token,))
    assert not lease_descendants.drained(receipt, lease_processes.exited)


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin kqueue fork guard")
def test_brief_parent_escape_becomes_unknown_instead_of_silent_release(home: Path) -> None:
    """A double-fork between polling ticks must leave the production lease fenced."""
    identity_file = home / "escaped.json"
    script = home / "brief_parent.py"
    _ = script.write_text(
        """import json, os, time
from pathlib import Path

child = os.fork()
if child == 0:
    grandchild = os.fork()
    if grandchild == 0:
        os.setsid()
        birth = __import__('psutil').Process(os.getpid()).create_time()
        Path(__import__('sys').argv[1]).write_text(json.dumps({'pid': os.getpid(), 'birth': birth}))
        time.sleep(30)
        os._exit(0)
    os._exit(0)
os.waitpid(child, 0)
deadline = time.monotonic() + 2
while not Path(__import__('sys').argv[1]).exists() and time.monotonic() < deadline:
    time.sleep(.01)
"""
    )
    receipt = lease_runtime.reserve(request(home))
    assert receipt is not None
    lease_runtime.arm(receipt.token)
    process = subprocess.Popen(
        [
            sys.executable,
            str(Path(lease_runtime.__file__).with_name("lease_runner.py")),
            str(home),
            receipt.token,
            str(script),
            str(identity_file),
        ],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    escaped: psutil.Process | None = None
    try:
        assert process.wait(timeout=10) == 0
        identity = json.loads(identity_file.read_text())
        escaped = psutil.Process(identity["pid"])
        assert escaped.create_time() == identity["birth"]
        current = lease_runtime.current(receipt.token)
        with lease_storage.readonly() as database:
            assert database is not None
            observation = lease_descendants.read(database, current)
        assert observation is not None
        assert observation.fork_guard == "darwin_kqueue"
        assert observation.state == "closed"
        assert any(item.pid == escaped.pid for item in observation.suspected_orphans)
        assert not lease_runtime.release(receipt.token)
    finally:
        if escaped is not None and escaped.is_running():
            escaped.kill()
            try:
                _ = escaped.wait(timeout=5)
            except psutil.TimeoutExpired:
                assert escaped.status() == psutil.STATUS_ZOMBIE
    assert lease_runtime.release(receipt.token)
