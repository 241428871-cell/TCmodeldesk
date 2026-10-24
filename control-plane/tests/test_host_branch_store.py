"""Branch rows live in the caller's project transaction and settle exactly once."""

import sqlite3
from collections.abc import Generator

import pytest

from model_desk_control.host_branch import BranchRef, branch_identity
from model_desk_control.host_branch_store import (
    BranchError,
    BranchMark,
    BranchStatus,
    live,
    mark,
    open_branch,
    read,
)


@pytest.fixture
def database() -> Generator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    try:
        yield connection
    finally:
        connection.close()


def ref(stage_id: str = "stage", *, epoch: int = 1, operation: str = "round-1") -> BranchRef:
    return BranchRef(
        project="/project",
        goal_id="goal",
        stage_id=stage_id,
        branch_id=branch_identity("/project", "goal", stage_id, operation, "dispatch:0"),
        epoch=epoch,
        origin_operation_id=operation,
    )


def test_reading_never_creates_branch_records(database: sqlite3.Connection) -> None:
    assert read(database, ref().branch_id) is None
    assert live(database, "goal") == ()
    tables = database.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    assert tables == []


def test_writes_require_the_callers_transaction(database: sqlite3.Connection) -> None:
    with pytest.raises(BranchError, match="branch_transaction_required"):
        _ = open_branch(database, ref())


def test_same_epoch_reuses_the_live_branch_and_newer_epoch_retires_it(
    database: sqlite3.Connection,
) -> None:
    _ = database.execute("BEGIN")
    first = open_branch(database, ref())
    again = open_branch(database, ref(operation="round-2"))
    assert again.reference == first.reference
    newer = open_branch(database, ref(epoch=2, operation="round-3"))
    assert newer.reference.epoch == 2
    previous = read(database, first.reference.branch_id)
    assert previous is not None
    assert previous.status is BranchStatus.RETIRED
    assert previous.reason == "epoch_changed"
    assert [row.reference.branch_id for row in live(database, "goal")] == [
        newer.reference.branch_id
    ]
    database.rollback()


def test_settled_outcome_is_immutable_but_replay_is_idempotent(
    database: sqlite3.Connection,
) -> None:
    _ = database.execute("BEGIN")
    branch = open_branch(database, ref()).reference.branch_id
    waiting = BranchMark(status=BranchStatus.WAITING, decision_id="decision")
    assert mark(database, branch, waiting).decision_id == "decision"
    done = BranchMark(status=BranchStatus.DISPATCHED, task_id="task-1")
    _ = mark(database, branch, done)
    assert mark(database, branch, done).task_id == "task-1"
    with pytest.raises(BranchError, match="branch_already_settled"):
        _ = mark(database, branch, BranchMark(status=BranchStatus.DISPATCHED, task_id="task-2"))
    with pytest.raises(BranchError, match="branch_already_settled"):
        _ = mark(database, branch, BranchMark(status=BranchStatus.RETIRED))
    database.rollback()


def test_waiting_requires_the_awaited_card(database: sqlite3.Connection) -> None:
    _ = database.execute("BEGIN")
    branch = open_branch(database, ref()).reference.branch_id
    with pytest.raises(BranchError, match="branch_wait_requires_decision"):
        _ = mark(database, branch, BranchMark(status=BranchStatus.WAITING))
    database.rollback()


def test_identity_conflict_is_rejected(database: sqlite3.Connection) -> None:
    _ = database.execute("BEGIN")
    original = open_branch(database, ref())
    forged = original.reference.model_copy(update={"stage_id": "other"})
    with pytest.raises(BranchError, match="branch_identity_conflict"):
        _ = open_branch(database, forged)
    database.rollback()
