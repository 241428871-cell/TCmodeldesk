"""Resource receipts across independent SQLite connections and stale owners."""

from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from sqlite3 import Connection, connect

import pytest
from pydantic import ValidationError

from model_desk_control import durable_leases as leases
from model_desk_control.durable_lease_models import DurableLease, LeasePhase, LeaseStoreError
from model_desk_control.host_queue_models import Owner, Process
from model_desk_control.resource_leases import LeaseDuplicateError, LeaseRequest


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Connection]:
    with closing(connect(tmp_path / "leases.sqlite")) as connection, connection:
        _ = connection.execute("BEGIN IMMEDIATE")
        yield connection


def holder(instance: str = "host_a") -> Owner:
    return Owner(instance=instance, process=Process(pid=101, birth=123.0))


def request(owner: str = "task_a", /, **changes: str | int | tuple[str, ...]) -> LeaseRequest:
    return LeaseRequest.model_validate(
        {
            "owner": owner,
            "project": "/project",
            "account": "shared",
            "files": ("src/a.py",),
            "project_limit": 4,
            "account_limit": 4,
            **changes,
        }
    )


def reserved(database: Connection) -> DurableLease:
    result = leases.reserve(database, request(), holder())
    assert result is not None
    return result


@pytest.mark.parametrize("phase", [LeasePhase.RESERVED, LeasePhase.ARMED, LeasePhase.STARTED])
def test_conflicts_remain_held_after_database_reopen(tmp_path: Path, phase: LeasePhase) -> None:
    # Given a committed reservation, possibly crossing the irreversible launch boundary.
    path = tmp_path / "leases.sqlite"
    with closing(connect(path)) as first, first:
        _ = first.execute("BEGIN IMMEDIATE")
        old = reserved(first)
        if phase in (LeasePhase.ARMED, LeasePhase.STARTED):
            old = leases.arm(first, old)
        if phase == LeasePhase.STARTED:
            _ = leases.started(first, old, Process(pid=102, birth=124.0))
    # When another controller opens the store after the original connection is gone.
    with closing(connect(path)) as second, second:
        _ = second.execute("BEGIN IMMEDIATE")
        result = leases.reserve(second, request("task_b"), holder("host_b"))
    # Then it cannot overlap the prior writer, regardless of wall-clock age.
    assert result is None


@pytest.mark.parametrize("phase", [LeasePhase.ARMED, LeasePhase.STARTED])
def test_owner_exit_never_reclaims_possible_children(
    database: Connection, phase: LeasePhase
) -> None:
    # Given a crossed launch boundary and an exited supervisor.
    old = leases.arm(database, reserved(database))
    if phase == LeasePhase.STARTED:
        old = leases.started(database, old, Process(pid=102, birth=124.0))
    # When the prelaunch recovery path is asked to retire that owner.
    reclaimed = leases.reclaim_unstarted(database, old, lambda _: True)
    # Then an unknown or surviving child still owns all resources.
    assert not reclaimed
    assert leases.read(database, old.token) == old


def test_unknown_owner_does_not_release_reserved_resources(database: Connection) -> None:
    # Given process inspection could not establish an exact exit.
    old = reserved(database)
    # When recovery receives the fail-closed verdict.
    reclaimed = leases.reclaim_unstarted(database, old, lambda _: False)
    # Then the reservation remains durable.
    assert not reclaimed
    assert leases.read(database, old.token) == old


def test_old_generation_cannot_release_replacement(database: Connection) -> None:
    # Given a prelaunch owner is proved exited and its original task is reserved anew.
    old = reserved(database)
    assert leases.reclaim_unstarted(database, old, lambda seen: seen == holder())
    replacement = reserved(database)
    assert replacement.token != old.token
    # When a stale callback tries to finish its old generation.
    with pytest.raises(LeaseStoreError, match="lease_transition_conflict"):
        _ = leases.finish(database, old, lambda _: True)
    # Then the replacement is still held.
    assert leases.active(database) == (replacement,)


@pytest.mark.parametrize("quiescent", [False, True])
def test_finish_requires_host_proof_for_exact_receipt(
    database: Connection, *, quiescent: bool
) -> None:
    # Given a started launcher, whose exit alone says nothing about descendants.
    armed = leases.arm(database, reserved(database))
    old = leases.started(database, armed, Process(pid=102, birth=124.0))
    seen: list[DurableLease] = []

    def verified(receipt: DurableLease) -> bool:
        seen.append(receipt)
        return quiescent

    # When the host's drain verifier runs inside the transaction.
    result = leases.finish(database, old, verified)
    # Then only a positive exact-receipt verdict releases the hold.
    assert seen == [old]
    assert result is quiescent
    assert leases.active(database) == (() if quiescent else (old,))


@pytest.mark.parametrize("birth", [None, 0.0, -1.0, float("nan"), float("inf")])
def test_missing_launcher_identity_keeps_armed_hold(
    database: Connection, birth: float | None
) -> None:
    # Given spawn may have happened but a trustworthy birth receipt is unavailable.
    old = leases.arm(database, reserved(database))
    # When the host attempts to acknowledge an unverifiable child.
    with pytest.raises(ValidationError, match="lease_process_identity_required"):
        _ = leases.started(database, old, Process(pid=102, birth=birth))
    # Then the armed receipt continues to block conflicting workers.
    assert leases.read(database, old.token) == old


def test_duplicate_task_is_rejected_across_hosts(database: Connection) -> None:
    # Given one host already owns the original task.
    _ = reserved(database)
    # When a second host presents the same project/task identity.
    with pytest.raises(LeaseDuplicateError):
        _ = leases.reserve(database, request(), holder("host_b"))
    # Then no second reservation was issued.
    assert len(leases.active(database)) == 1


@pytest.mark.parametrize("constraint", ["project_limit", "account_limit"])
def test_new_larger_limit_cannot_override_existing_hold(
    database: Connection, constraint: str
) -> None:
    # Given a worker admitted under a stricter concurrency setting.
    first = leases.reserve(database, request(**{constraint: 1}), holder())
    assert first is not None
    # When another instance sees a raised setting and requests disjoint work.
    result = leases.reserve(database, request("task_b", files=("src/b.py",)), holder("host_b"))
    # Then the old limit continues to apply until that reservation is released.
    assert result is None


def test_nested_project_paths_conflict_across_hosts(database: Connection) -> None:
    # Given a parent project has reserved a file in a nested checkout.
    first = leases.reserve(database, request(files=("child/src/a.py",)), holder())
    assert first is not None
    # When the nested project requests that same physical relative path.
    result = leases.reserve(database, request("task_b", project="/project/child"), holder("host_b"))
    # Then different project keys cannot bypass path exclusion.
    assert result is None


def test_inspection_is_read_only_and_mutation_requires_transaction(tmp_path: Path) -> None:
    # Given a fresh store with no schema.
    with closing(connect(tmp_path / "empty.sqlite")) as connection:
        # When inspecting or attempting an unfenced write.
        assert leases.active(connection) == ()
        assert leases.read(connection, "missing") is None
        with pytest.raises(LeaseStoreError, match="lease_transaction_required"):
            _ = leases.reserve(connection, request(), holder())
        # Then no schema or implicit transaction has appeared.
        assert connection.execute("SELECT name FROM sqlite_schema").fetchone() is None
        assert not connection.in_transaction


def test_rollback_does_not_consume_resources(tmp_path: Path) -> None:
    # Given an interrupted admission transaction.
    with closing(connect(tmp_path / "rollback.sqlite")) as connection:
        _ = connection.execute("BEGIN IMMEDIATE")
        _ = reserved(connection)
        connection.rollback()
        # When the next operation checks the store.
        held = leases.active(connection)
        # Then only committed reservations can block it.
        assert held == ()


def test_corrupt_active_receipt_blocks_even_disjoint_work(database: Connection) -> None:
    # Given an active receipt whose indexed phase contradicts its saved data.
    old = reserved(database)
    _ = database.execute("UPDATE resource_leases SET phase='armed' WHERE token=?", (old.token,))
    # When another host asks for a different file.
    with pytest.raises(LeaseStoreError, match="lease_receipt_identity"):
        _ = leases.reserve(database, request("task_b", files=("b.py",)), holder("host_b"))
    # Then no guessed release or replacement reservation is committed.
    assert database.execute("SELECT count(*) FROM resource_leases").fetchone() == (1,)


def test_failed_release_transaction_keeps_resources(tmp_path: Path) -> None:
    # Given a committed armed reservation.
    with closing(connect(tmp_path / "release.sqlite")) as connection:
        with connection:
            _ = connection.execute("BEGIN IMMEDIATE")
            old = leases.arm(connection, reserved(connection))
        # When a verified release is interrupted before commit.
        _ = connection.execute("BEGIN IMMEDIATE")
        assert leases.finish(connection, old, lambda _: True)
        connection.rollback()
        # Then the previous hold is still authoritative on disk.
        assert leases.read(connection, old.token) == old
