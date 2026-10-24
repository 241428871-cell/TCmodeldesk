import threading
import time
from collections.abc import Callable

import pytest

from model_desk_control.resource_leases import (
    LeaseBroker,
    LeaseCancelled,
    LeaseRequest,
    conflicting_path,
    normalize_path,
)

NEVER = lambda: False  # noqa: E731


CONFLICT = "文件冲突："
WIDE = 3


def test_recovery_try_acquire_never_waits_or_leaves_a_ticket() -> None:
    broker = LeaseBroker()
    first = LeaseRequest(owner="first", project="project", account="original")
    second = LeaseRequest(owner="recovery", project="project", account="file-recovery")
    assert broker.try_acquire(first)
    assert not broker.try_acquire(second)
    assert broker.state().waiting == ()
    broker.release(first.owner)
    assert broker.try_acquire(second)
    broker.release(second.owner)
    assert broker.state().held == ()


def request(
    owner: str,
    *files: str,
    project: str = "p",
    account: str = "a",
    wide: bool = False,
) -> LeaseRequest:
    limit = WIDE if wide else 1
    return LeaseRequest(
        owner=owner,
        project=project,
        account=account,
        files=files,
        project_limit=limit,
        account_limit=limit,
    )


def wait_for(condition: Callable[[], bool], seconds: float = 3.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


def start(
    broker: LeaseBroker, item: LeaseRequest, cancel: threading.Event | None = None
) -> threading.Thread:
    event = cancel or threading.Event()
    thread = threading.Thread(target=lambda: _run(broker, item, event), daemon=True)
    thread.start()
    return thread


def _run(broker: LeaseBroker, item: LeaseRequest, cancel: threading.Event) -> None:
    try:
        broker.acquire(item, cancel.is_set)
    except LeaseCancelled:
        return


def test_paths_compare_by_containment_not_by_prefix_text() -> None:
    assert normalize_path("./Src//A.py") == "src/a.py"
    assert conflicting_path(request("a", "src"), request("b", "src/x.py")) == "src"
    assert conflicting_path(request("a", "src/x.py"), request("b", "src/y.py")) == ""
    assert conflicting_path(request("a", "src"), request("b", "srcs/x.py")) == ""
    assert conflicting_path(request("a", "A.PY"), request("b", "a.py")) == "A.PY"


def test_read_only_workers_never_conflict_and_other_projects_are_independent() -> None:
    assert conflicting_path(request("a"), request("b", "x")) == ""
    assert conflicting_path(request("a", "x"), request("b", "x", project="other")) == ""
    wide = LeaseRequest(owner="w", project="p", account="a", exclusive=True)
    assert conflicting_path(wide, request("b", "x")) == "整个项目"


def test_default_limit_is_one_worker_per_project() -> None:
    broker = LeaseBroker()
    broker.acquire(request("first", "a.py"), NEVER)
    _ = start(broker, request("second", "b.py", account="b"))
    assert wait_for(lambda: broker.state().waiting == (("second", "项目并发已满"),))
    broker.release("first")
    assert wait_for(lambda: broker.state().held == ("second",))


def test_raised_limit_runs_disjoint_files_together_but_refuses_overlap() -> None:
    broker = LeaseBroker()
    broker.acquire(request("one", "src/a.py", wide=True), NEVER)
    broker.acquire(request("two", "src/b.py", wide=True), NEVER)
    _ = start(broker, request("three", "src", wide=True))
    assert wait_for(lambda: broker.state().waiting == (("three", CONFLICT + "src"),))
    assert broker.state().held == ("one", "two")
    broker.release("one")
    assert broker.state().waiting == (("three", CONFLICT + "src"),)
    broker.release("two")
    assert wait_for(lambda: broker.state().held == ("three",))


def test_account_limit_holds_workers_from_different_projects() -> None:
    broker = LeaseBroker()
    broker.acquire(request("a1", project="p1"), NEVER)
    _ = start(broker, request("a2", project="p2"))
    assert wait_for(lambda: broker.state().waiting == (("a2", "账户并发已满"),))
    other = request("b1", project="p3", account="other")
    broker.acquire(other, NEVER)
    assert broker.state().held == ("a1", "b1")


def test_a_blocked_earlier_worker_keeps_its_place_against_later_conflicts() -> None:
    broker = LeaseBroker()
    broker.acquire(request("holder", "x.py", wide=True), NEVER)
    _ = start(broker, request("early", "x.py", "y.py", wide=True))
    assert wait_for(lambda: len(broker.state().waiting) == 1)
    _ = start(broker, request("late", "y.py", wide=True))
    assert wait_for(lambda: len(broker.state().waiting) == 2)
    time.sleep(0.4)
    assert broker.state().held == ("holder",)
    broker.release("holder")
    assert wait_for(lambda: broker.state().held == ("early",))
    assert dict(broker.state().waiting)["late"].startswith(CONFLICT)


def test_cancelled_waiter_leaves_no_lease_and_unblocks_nobody_wrongly() -> None:
    broker = LeaseBroker()
    broker.acquire(request("holder"), NEVER)
    cancel = threading.Event()
    thread = start(broker, request("waiter"), cancel)
    assert wait_for(lambda: len(broker.state().waiting) == 1)
    cancel.set()
    thread.join(3)
    assert broker.state().waiting == ()
    assert broker.state().held == ("holder",)


def test_release_is_idempotent_and_duplicate_owner_is_refused() -> None:
    broker = LeaseBroker()
    broker.release("missing")
    broker.acquire(request("same"), NEVER)
    with pytest.raises(ValueError, match="重复"):
        broker.acquire(request("same"), NEVER)
    broker.release("same")
    broker.release("same")
    assert broker.state().held == ()


def test_nested_project_cannot_lease_a_file_already_owned_by_parent() -> None:
    parent = request("parent", "nested/a.py", project="/workspace")
    child = request("child", "a.py", project="/workspace/nested")
    assert conflicting_path(parent, child)


def test_exclusive_parent_conflicts_with_nested_project_write() -> None:
    parent = LeaseRequest(owner="parent", project="/workspace", account="a", exclusive=True)
    child = request("child", "a.py", project="/workspace/nested")
    assert conflicting_path(parent, child)
