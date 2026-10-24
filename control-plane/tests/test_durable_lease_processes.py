"""Independent local Python processes contend for one temporary resource database."""

import subprocess
import sys
from contextlib import ExitStack, closing
from pathlib import Path
from sqlite3 import connect
from typing import Final

import pytest

from model_desk_control import durable_leases as leases
from model_desk_control.durable_lease_models import LeasePhase

CHILD: Final = """
import os
import sys
from contextlib import closing
from sqlite3 import connect
from model_desk_control import durable_leases as leases
from model_desk_control.host_queue_models import Owner, Process
from model_desk_control.resource_leases import LeaseRequest

with closing(connect(sys.argv[1], timeout=10)) as database, database:
    print('ready', flush=True)
    assert sys.stdin.readline().strip() == 'go'
    database.execute('BEGIN IMMEDIATE')
    holder = Owner(instance='host_' + sys.argv[2], process=Process(pid=os.getpid(), birth=1.0))
    request = LeaseRequest(owner='task_' + sys.argv[2], project='/temporary/project',
        account=sys.argv[3], files=(sys.argv[4],), project_limit=4, account_limit=1)
    result = leases.reserve(database, request, holder)
    if result is not None and sys.argv[5] == 'armed':
        result = leases.arm(database, result)
    database.commit()
    print('blocked' if result is None else result.token, flush=True)
"""


@pytest.mark.parametrize(
    ("second_account", "second_file", "accepted"),
    [("other", "a.py", 1), ("shared", "b.py", 1), ("other", "b.py", 2)],
)
def test_atomic_admission_between_processes(
    tmp_path: Path, second_account: str, second_file: str, accepted: int
) -> None:
    # Given two independent controllers, with no shared Python lock or broker.
    path = tmp_path / "shared.sqlite"
    with ExitStack() as stack:
        workers = [
            stack.enter_context(
                subprocess.Popen(  # noqa: S603 - fixed local Python, no shell.
                    [sys.executable, "-c", CHILD, str(path), identity, account, file, "reserved"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
            for identity, account, file in [
                ("a", "shared", "a.py"),
                ("b", second_account, second_file),
            ]
        ]
        # When both are allowed to contend without waiting for the other to exit.
        try:
            for worker in workers:
                assert worker.stdin is not None
                _ = worker.stdin.write("go\n")
                worker.stdin.flush()
            outputs = [worker.communicate(timeout=15) for worker in workers]
        finally:
            for worker in workers:
                if worker.poll() is None:
                    worker.kill()
                    _ = worker.communicate(timeout=5)
        # Then only nonconflicting resources can be admitted.
        assert all(worker.returncode == 0 for worker in workers), outputs
        assert sum(output.splitlines()[-1] != "blocked" for output, _ in outputs) == accepted
    with closing(connect(path)) as database:
        assert len(leases.active(database)) == accepted


def test_process_exit_after_arm_is_not_a_release(tmp_path: Path) -> None:
    # Given a real controller exits after durable arming, with no launcher receipt.
    path = tmp_path / "armed.sqlite"
    exited = subprocess.run(  # noqa: S603 - fixed local Python, no shell or model executable.
        [sys.executable, "-c", CHILD, str(path), "a", "shared", "a.py", "armed"],
        input="go\n",
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    token = exited.stdout.splitlines()[-1]
    # When the next process asks for the same file under a different account.
    contender = subprocess.run(  # noqa: S603 - same isolated local protocol exercise.
        [sys.executable, "-c", CHILD, str(path), "b", "other", "a.py", "reserved"],
        input="go\n",
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    # Then a missing spawn acknowledgement cannot turn into duplicate execution.
    assert contender.stdout.splitlines()[-1] == "blocked"
    with closing(connect(path)) as database:
        receipt = leases.read(database, token)
        assert receipt is not None
        assert receipt.phase == LeasePhase.ARMED
