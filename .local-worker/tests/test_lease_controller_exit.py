"""A real controller exits while its registered production bootstrap remains alive."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Final

import psutil
import pytest

from model_desk_control.durable_lease_models import LeaseStoreError
from model_desk_control.host_flow import HostRecord

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lease_processes
import lease_runtime
from test_lease_runtime import home as home
from test_lease_runtime import request

CONTROLLER: Final = """
import json, os, subprocess, sys, psutil
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import control_bootstrap
control_bootstrap.bind_source()
import model_registry as registry
import lease_runtime
from model_desk_control.resource_leases import LeaseRequest
registry.HERE = Path(sys.argv[2])
request = LeaseRequest(owner='original', project=sys.argv[2], account='fixture', files=('a.py',))
receipt = lease_runtime.reserve(request)
assert receipt is not None
lease_runtime.arm(receipt.token)
process = subprocess.Popen([sys.executable, str(Path(sys.argv[1]) / 'lease_runner.py'),
    sys.argv[2], receipt.token, sys.argv[3]], start_new_session=True,
    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
identity = json.dumps({'pid': process.pid, 'birth': psutil.Process(process.pid).create_time(),
    'token': receipt.token})
(registry.HERE / 'child.json').write_text(identity)
assert process.stdout.readline().strip() == 'registered'
print(identity, flush=True)
os._exit(0)
"""


class Child(HostRecord):
    pid: int
    birth: float
    token: str


@pytest.mark.skipif(os.name == "nt", reason="POSIX session recovery after real controller exit")
@pytest.mark.parametrize("graceful", [False, True])
def test_controller_exit_does_not_free_live_worker_and_takeover_waits_for_it(
    home: Path, *, graceful: bool
) -> None:
    # Given a separate controller starts the production bootstrap then exits abruptly.
    script = home / "worker.py"
    _ = script.write_text(
        """import signal, sys
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
print('registered', flush=True)
signal.pause()
"""
    )
    try:
        completed = subprocess.run(  # noqa: S603 - fixed local Python and isolated fixture argv.
            [
                sys.executable,
                "-c",
                CONTROLLER,
                str(Path(lease_runtime.__file__).parent),
                str(home),
                str(script),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        child = Child.model_validate_json(completed.stdout)
        original = lease_runtime.current(child.token)
        assert lease_processes.exited(original.holder.process)
        # When a new controller asks for the same resources while the worker is alive.
        assert lease_runtime.reserve(request(home, "second")) is None
        with pytest.raises(LeaseStoreError, match="lease_recovery_not_quiescent"):
            _ = lease_runtime.takeover(original.request, original.token)
        # Then neither controller exit nor a new owner can bypass the running worker.
        assert lease_runtime.current(child.token) == original
    finally:
        saved = home / "child.json"
        if saved.exists():
            child = Child.model_validate_json(saved.read_bytes())
            process = psutil.Process(child.pid)
            assert abs(process.create_time() - child.birth) < 0.01
            if graceful:
                process.terminate()
            else:
                process.kill()
            try:
                _ = process.wait(timeout=5)
            except psutil.TimeoutExpired:
                assert process.status() == psutil.STATUS_ZOMBIE

    if graceful:
        replacement = lease_runtime.takeover(request(home), child.token)
        assert replacement.token != child.token
        assert lease_runtime.release(replacement.token)
    else:
        # SIGKILL loses the final descendant scan; an empty SID is insufficient.
        with pytest.raises(LeaseStoreError, match="lease_recovery_not_quiescent"):
            _ = lease_runtime.takeover(request(home), child.token)
