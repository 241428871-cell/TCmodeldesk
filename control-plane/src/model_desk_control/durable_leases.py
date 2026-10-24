"""Transactional resource arbitration shared by hosts; no timer or process launch."""

from collections.abc import Callable
from sqlite3 import Connection
from typing import Final, Literal
from uuid import uuid4

from model_desk_control.database import text_cursor
from model_desk_control.durable_lease_models import DurableLease, LeasePhase, LeaseStoreError
from model_desk_control.host_events import exists
from model_desk_control.host_queue_models import Owner, Process
from model_desk_control.resource_leases import LeaseDuplicateError, LeaseRequest, blocking_reason

MAX_ACTIVE: Final = 1024
MAX_RECEIPT: Final = 64 * 1024


def _transaction(database: Connection) -> None:
    if not database.in_transaction:
        code = "lease_transaction_required"
        raise LeaseStoreError(code)


def _parse(token: str, phase: str, data: str) -> DurableLease:
    if len(data.encode()) > MAX_RECEIPT:
        code = "lease_receipt_budget"
        raise LeaseStoreError(code)
    result = DurableLease.model_validate_json(data)
    if (result.token, result.phase.value) != (token, phase):
        code = "lease_receipt_identity"
        raise LeaseStoreError(code)
    return result


def read(database: Connection, token: str) -> DurableLease | None:
    """Inspection neither initializes a schema nor changes a reservation."""
    if not exists(database, "resource_leases"):
        return None
    row = text_cursor(
        database,
        "SELECT token,phase,substr(data,1,65537) FROM resource_leases WHERE token=?",
        (token,),
    ).fetchone()
    return None if row is None else _parse(row[0], row[1], row[2])


def active(database: Connection) -> tuple[DurableLease, ...]:
    """Bound inspection cost; overflow or corrupt receipts deny further admission."""
    if not exists(database, "resource_leases"):
        return ()
    rows = text_cursor(
        database,
        """SELECT token,phase,substr(data,1,65537) FROM resource_leases
        WHERE phase!='released' ORDER BY rowid LIMIT 1025""",
    ).fetchall()
    if len(rows) > MAX_ACTIVE:
        code = "lease_active_budget"
        raise LeaseStoreError(code)
    return tuple(_parse(row[0], row[1], row[2]) for row in rows)


def _write(database: Connection, state: DurableLease, previous: DurableLease | None) -> None:
    _transaction(database)
    data = state.model_dump_json()
    if len(data.encode()) > MAX_RECEIPT:
        code = "lease_receipt_budget"
        raise LeaseStoreError(code)
    if previous is None:
        _ = database.execute("""CREATE TABLE IF NOT EXISTS resource_leases (
            token TEXT PRIMARY KEY, phase TEXT NOT NULL, data TEXT NOT NULL
            CHECK(json_valid(data)))""")
        _ = database.execute(
            "INSERT INTO resource_leases VALUES(?,?,?)", (state.token, state.phase.value, data)
        )
    else:
        raw = text_cursor(
            database,
            "SELECT data FROM resource_leases WHERE token=?", (previous.token,)
        ).fetchone()
        if raw is None or DurableLease.model_validate_json(raw[0]) != previous:
            code = "lease_transition_conflict"
            raise LeaseStoreError(code)
        changed = database.execute(
            "UPDATE resource_leases SET phase=?,data=? WHERE token=? AND data=?",
            (state.phase.value, data, previous.token, raw[0]),
        )
        if changed.rowcount != 1:
            code = "lease_transition_conflict"
            raise LeaseStoreError(code)


def reserve(database: Connection, request: LeaseRequest, holder: Owner) -> DurableLease | None:
    """Caller holds BEGIN IMMEDIATE across admission and commit, before any spawn."""
    _transaction(database)
    state = DurableLease(token="lease_" + uuid4().hex, request=request, holder=holder)
    held = active(database)
    if any(
        item.request.owner == request.owner and item.request.project == request.project
        for item in held
    ):
        raise LeaseDuplicateError(request.owner)
    if len(held) >= MAX_ACTIVE:
        code = "lease_active_budget"
        raise LeaseStoreError(code)
    requests = [item.request for item in held]
    if blocking_reason(request, requests, []):
        return None
    # An older reservation's smaller limit remains binding until it releases.
    for item in requests:
        if (
            item.project == request.project
            and sum(other.project == request.project for other in requests) >= item.project_limit
        ):
            return None
        if (
            item.account == request.account
            and sum(other.account == request.account for other in requests) >= item.account_limit
        ):
            return None
        if (request.model and item.model == request.model
                and sum(other.model == request.model for other in requests) >= item.model_limit):
            return None
    _write(database, state, None)
    return state


def _current(database: Connection, expected: DurableLease) -> None:
    _transaction(database)
    if read(database, expected.token) != expected:
        code = "lease_transition_conflict"
        raise LeaseStoreError(code)


def arm(database: Connection, expected: DurableLease) -> DurableLease:
    """Commit before Popen; a crash after this point holds resources indefinitely."""
    _current(database, expected)
    if expected.phase != LeasePhase.RESERVED:
        code = "lease_not_reserved"
        raise LeaseStoreError(code)
    state = expected.model_copy(update={"phase": LeasePhase.ARMED})
    _write(database, state, expected)
    return state


def started(
    database: Connection,
    expected: DurableLease,
    launcher: Process,
    *,
    containment: Literal["none", "posix_session"] = "none",
) -> DurableLease:
    """Persist the exact child identity; missing birth time leaves the armed hold intact."""
    _current(database, expected)
    if expected.phase != LeasePhase.ARMED:
        code = "lease_not_armed"
        raise LeaseStoreError(code)
    state = DurableLease(
        token=expected.token,
        request=expected.request,
        holder=expected.holder,
        phase=LeasePhase.STARTED,
        launcher=launcher,
        containment=containment,
    )
    _write(database, state, expected)
    return state


def reclaim_unstarted(
    database: Connection, expected: DurableLease, exited: Callable[[Owner], bool]
) -> bool:
    """Only RESERVED plus exact holder exit permits recovery; UNKNOWN must return False."""
    _current(database, expected)
    if expected.phase != LeasePhase.RESERVED or not exited(expected.holder):
        return False
    _write(database, expected.model_copy(update={"phase": LeasePhase.RELEASED}), expected)
    return True


def finish(
    database: Connection, expected: DurableLease, quiescent: Callable[[DurableLease], bool]
) -> bool:
    """Host must prove all effects stopped, not merely owner/launcher exit or TTL expiry.

    The verifier runs under the transaction against this exact receipt. RESERVED may
    be cancelled by its current holder before arming; ARMED needs proven spawn failure
    or external reconciliation; STARTED needs a drained process tree and writeback.
    An unreadable process tree must return False. This is not authorization to retry.
    """
    _current(database, expected)
    if expected.phase == LeasePhase.RELEASED:
        return True
    if not quiescent(expected):
        return False
    _write(database, expected.model_copy(update={"phase": LeasePhase.RELEASED}), expected)
    return True
