"""Local waiting queues combined with the installation's durable resource reservations."""

import os
import threading
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlparse

import continuation_clock
import control_bootstrap
import model_registry as registry
from pydantic import BaseModel, ConfigDict, Field, JsonValue

control_bootstrap.bind_source()
import lease_runtime
import lease_storage

from model_desk_control import durable_leases
from model_desk_control.resource_leases import (
    LeaseBroker,
    LeaseCancelled,
    LeaseDuplicateError,
    LeaseRequest,
    LeaseState,
)

type Record = dict[str, JsonValue]

BROKER = LeaseBroker()
LOCK = threading.RLock()
WAITING: dict[str, str] = {}


@dataclass(frozen=True, slots=True)
class Hold:
    """Local callback ownership only; durable arbitration lives in SQLite."""

    token: str
    run: Path


HELD: dict[str, Hold] = {}
__all__ = [
    "BROKER",
    "LeaseCancelled",
    "LeaseDuplicateError",
    "account_key",
    "acquire",
    "release",
    "request_for",
    "state",
    "validate",
    "validate_task",
]


class LeaseTask(BaseModel):
    """Parse only the original task fields needed for resource and STOP checks."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="ignore")
    id: str
    project: str = ""
    kind: str = "chat"
    files: list[str] = Field(default_factory=list)
    goal_id: str | None = None
    authority_epoch: int | None = None


class LeaseModel(BaseModel):
    """Use conservative provider/login or host buckets without retaining credentials."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="ignore")
    provider: str = "unknown"
    api_base: str = ""
    id: str = ""
    model: str = ""


def account_key(model: Record) -> str:
    """Native logins share one slot; API hosts conservatively share one slot."""
    parsed = LeaseModel.model_validate(model)
    provider = parsed.provider
    if provider in {"claude_cli", "codex_cli"}:
        return provider
    host = urlparse(parsed.api_base).hostname
    if provider == "local" or not host:
        return provider + ":" + (parsed.id or parsed.model)
    return provider + "@" + host


def request_for(state: Record, model: Record) -> LeaseRequest:
    """Resolve file aliases before comparing writers, including nested projects."""
    import workspace_config

    task = LeaseTask.model_validate(state)
    project = Path(task.project).resolve() if task.project else None
    writes = task.kind == "edit"
    files = (
        tuple((project / name).resolve().relative_to(project).as_posix() for name in task.files)
        if writes and project
        else ()
    )
    limit = workspace_config.work_concurrency(str(project)) if project else 1
    return LeaseRequest(
        owner=task.id,
        project=str(project) if project else "(unbound)",
        account=account_key(model),
        files=files,
        exclusive=writes and not files,
        project_limit=limit,
    )


def acquire(request: LeaseRequest, run: Path) -> None:
    """Release a waiting slot promptly on cancellation or current goal revocation."""
    task = LeaseTask.model_validate_json((run / "task.json").read_bytes())
    flag = run / "cancelled.flag"

    def cancelled() -> bool:
        if flag.exists():
            return True
        if not task.goal_id:
            return False
        return not continuation_clock.dispatch_guard(
            task.project, task.goal_id, authority_epoch=task.authority_epoch
        )["allowed"]

    BROKER.acquire(request, cancelled)
    acquired = False
    try:
        while True:
            if cancelled():
                raise LeaseCancelled(request.owner)
            receipt = lease_runtime.reserve(request)
            if receipt is not None:
                with LOCK:
                    HELD[request.owner] = Hold(receipt.token, run)
                    _ = WAITING.pop(request.owner, None)
                _ = registry.patch_task(
                    run, execution_lease_token=receipt.token, execution_lease_protocol=1
                )
                acquired = True
                return
            with LOCK:
                WAITING[request.owner] = "等待跨进程项目、账户或文件租约"
            _ = threading.Event().wait(0.25)
    finally:
        if not acquired:
            release(request.owner)


def release(owner: str) -> None:
    """Release this task's lease after its original executor has returned."""
    with LOCK:
        hold = HELD.pop(owner, None)
        _ = WAITING.pop(owner, None)
    try:
        if hold is not None:
            try:
                released = lease_runtime.release(hold.token)
            except (OSError, ValueError):
                _pending(hold)
                raise
            if not released:
                _pending(hold)
    finally:
        BROKER.release(owner)


def _pending(hold: Hold) -> None:
    _ = registry.patch_task(
        hold.run,
        status="cancelled" if (hold.run / "cancelled.flag").exists() else "recovery_pending",
        phase="待核对(子进程静止未确认)",
        recovery={
            "kind": "resource_lease",
            "auto_resend": False,
            "reason": "保留持久租约，尚未证明工作进程及其会话已静止",
        },
    )


def launch(command: list[str], task_id: str) -> list[str]:
    """Commit before spawn and route the unchanged worker through its receipt bootstrap."""
    with LOCK:
        hold = HELD.get(task_id)
    if hold is None:
        raise LeaseChangedError
    lease_runtime.arm(hold.token)
    return [
        command[0],
        str(Path(__file__).with_name("lease_runner.py")),
        str(registry.HERE.resolve()),
        hold.token,
        *command[1:],
    ]


def spawn_failed(task_id: str) -> None:
    """Only the original Popen failure path may certify that no child was created."""
    with LOCK:
        hold = HELD.get(task_id)
    if hold is not None:
        _ = lease_runtime.release(hold.token, spawn_failed=True)


@contextmanager
def recovery(request: LeaseRequest, run: Path, previous_token: str) -> Generator[None]:
    """Take over durable resources without releasing them between recovery steps."""
    if not BROKER.try_acquire(request):
        raise LeaseDuplicateError(request.owner)
    token = ""
    try:
        receipt = lease_runtime.takeover(request, previous_token)
        token = receipt.token
        _ = registry.patch_task(run, execution_lease_token=token, execution_lease_protocol=1)
        yield
    finally:
        try:
            if token:
                _ = lease_runtime.release(token)
        finally:
            BROKER.release(request.owner)


class LeaseChangedError(ValueError):
    """The running worker must use the same resources its parent reserved."""

    def __init__(self) -> None:
        """Keep diagnostics independent of connection credentials."""
        super().__init__("排队任务的资源身份或并发设置已改变，未使用旧租约启动；请核对原任务")


def validate(request: LeaseRequest, task: Record, model: Record) -> None:
    """Recheck current aliases, account bucket and capacity after waiting."""
    if request != request_for(task, model):
        raise LeaseChangedError


def validate_task(task: Record, model: Record) -> None:
    """Check a parent's lease snapshot against the worker's actual inputs.

    Protocol tasks also prove their exact launcher and token in the shared store.
    Legacy/direct workers without a protocol retain their existing admission contract.
    """
    token = task.get("execution_lease_token")
    if (
        token is not None
        or task.get("execution_lease_protocol")
        or os.environ.get("AAD_LEASE_TOKEN")
    ):
        if not isinstance(token, str) or not token:
            raise LeaseChangedError
        lease_runtime.verify(token, request_for(task, model))
    if "execution_lease" not in task:
        return
    snapshot = task["execution_lease"]
    if not isinstance(snapshot, str):
        raise LeaseChangedError
    validate(LeaseRequest.model_validate_json(snapshot), task, model)


def state() -> LeaseState:
    """Read only; never create records or dispatch a worker."""
    local = BROKER.state()
    with lease_storage.readonly() as database:
        remote = durable_leases.active(database) if database is not None else ()
    with LOCK:
        waiting = dict(local.waiting) | WAITING
        local_held = set(local.held) - set(WAITING)
    return LeaseState(
        held=tuple(sorted(local_held | {item.request.owner for item in remote})),
        waiting=tuple(sorted(waiting.items())),
    )
