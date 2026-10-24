"""Durable admission and lifecycle for the original executor and file-only recovery."""

import os
from pathlib import Path
from typing import Final
from uuid import uuid4

import lease_descendants
import lease_processes
import lease_storage
import platform_runtime

from model_desk_control import durable_leases as leases
from model_desk_control.durable_lease_models import DurableLease, LeasePhase, LeaseStoreError
from model_desk_control.host_queue_models import Owner, Process
from model_desk_control.resource_leases import LeaseRequest

INSTANCE: Final = "lease_host_" + uuid4().hex


def owner() -> Owner:
    """Capture this controller incarnation, never just its reusable PID."""
    return Owner(
        instance=INSTANCE,
        process=Process(pid=os.getpid(), birth=platform_runtime.process_birth(os.getpid())),
    )


def reserve(request: LeaseRequest) -> DurableLease | None:
    """Reconcile provably idle other tasks and atomically attempt admission."""
    with lease_storage.transaction() as database:
        for previous in leases.active(database):
            if previous.phase != LeasePhase.RESERVED and (
                previous.request.project,
                previous.request.owner,
            ) == (
                request.project,
                request.owner,
            ):
                continue
            if not leases.reclaim_unstarted(
                database, previous, lambda old: lease_processes.exited(old.process)
            ):
                _ = leases.finish(database, previous, lease_processes.quiet)
        return leases.reserve(database, request, owner())


def current(token: str) -> DurableLease:
    """Missing receipts are a refusal, never a legacy fallback."""
    with lease_storage.readonly() as database:
        value = leases.read(database, token) if database is not None else None
    if value is None:
        code = "lease_receipt_missing"
        raise LeaseStoreError(code)
    return value


def arm(token: str) -> None:
    """Commit the irrevocable launch boundary before the original Popen."""
    with lease_storage.transaction() as database:
        previous = leases.read(database, token)
        if previous is None or previous.holder != owner():
            code = "lease_holder_changed"
            raise LeaseStoreError(code)
        _ = leases.arm(database, previous)


def enter(home: Path, token: str) -> DurableLease:
    """Worker self-registers before loading an executor, closing the parent ACK gap."""
    process = Process(pid=os.getpid(), birth=platform_runtime.process_birth(os.getpid()))
    if os.name != "nt" and os.getsid(process.pid) != process.pid:
        code = "lease_worker_session_invalid"
        raise LeaseStoreError(code)
    with lease_storage.transaction(home) as database:
        previous = leases.read(database, token)
        if previous is None:
            code = "lease_receipt_missing"
            raise LeaseStoreError(code)
        receipt = leases.started(
            database, previous, process, containment="posix_session" if os.name != "nt" else "none"
        )
        lease_descendants.initialize(database, token, process)
        return receipt


def release(token: str, *, spawn_failed: bool = False) -> bool:
    """Keep uncertain children fenced even when the original callback has returned."""
    with lease_storage.transaction() as database:
        previous = leases.read(database, token)
        if previous is None:
            code = "lease_receipt_missing"
            raise LeaseStoreError(code)
        own = previous.holder == owner()
        return leases.finish(
            database,
            previous,
            lambda item: (
                (own and item.phase == LeasePhase.RESERVED)
                or (own and spawn_failed and item.phase == LeasePhase.ARMED)
                or lease_processes.quiet(item)
            ),
        )


def verify(token: str, request: LeaseRequest) -> None:
    """A child can use only its own current launcher receipt and exact resource snapshot."""
    home = os.environ.get("AAD_LEASE_HOME", "")
    if not home or os.environ.get("AAD_LEASE_TOKEN") != token:
        code = "lease_worker_context_missing"
        raise LeaseStoreError(code)
    with lease_storage.readonly(Path(home)) as database:
        saved = leases.read(database, token) if database is not None else None
    process = Process(pid=os.getpid(), birth=platform_runtime.process_birth(os.getpid()))
    if (
        saved is None
        or saved.phase != LeasePhase.STARTED
        or saved.launcher != process
        or saved.request != request
    ):
        code = "lease_worker_ownership_changed"
        raise LeaseStoreError(code)


def takeover(request: LeaseRequest, previous_token: str) -> DurableLease:
    """Exchange a quiescent original hold for recovery ownership in one transaction."""
    with lease_storage.transaction() as database:
        previous = leases.read(database, previous_token) if previous_token else None
        if previous_token and previous is None:
            code = "lease_receipt_missing"
            raise LeaseStoreError(code)
        if previous is not None:
            if (
                previous.request.owner,
                previous.request.project,
                previous.request.files,
                previous.request.exclusive,
            ) != (request.owner, request.project, request.files, request.exclusive):
                code = "lease_recovery_scope_changed"
                raise LeaseStoreError(code)
            if not leases.finish(database, previous, recoverable):
                code = "lease_recovery_not_quiescent"
                raise LeaseStoreError(code)
        # A recovery process may die after reserving but before recording its new token.
        # Only its exact exit can retire that otherwise unreachable, unstarted generation.
        for orphan in leases.active(database):
            if (orphan.request.project, orphan.request.owner) == (request.project, request.owner):
                _ = leases.reclaim_unstarted(
                    database, orphan, lambda old: lease_processes.exited(old.process)
                )
        result = leases.reserve(database, request, owner())
        if result is None:
            code = "lease_recovery_resources_busy"
            raise LeaseStoreError(code)
        return result


def recoverable(receipt: DurableLease) -> bool:
    """An interrupted inline recovery has no children; its exact owner exit suffices."""
    return (
        receipt.phase == LeasePhase.RESERVED and lease_processes.exited(receipt.holder.process)
    ) or lease_processes.quiet(receipt)


def reconcile(token: str, project: str, task_id: str) -> bool:
    """Retire only proven idle resources for the exact original task, without dispatch."""
    with lease_storage.transaction() as database:
        receipt = leases.read(database, token)
        if receipt is None or (receipt.request.project, receipt.request.owner) != (
            project,
            task_id,
        ):
            code = "lease_reconcile_identity_changed"
            raise LeaseStoreError(code)
        return leases.finish(database, receipt, recoverable)
