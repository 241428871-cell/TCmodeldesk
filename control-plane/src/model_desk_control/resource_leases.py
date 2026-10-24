"""Bounded parallel workers: project/account limits and write-file conflicts, with no I/O."""

import posixpath
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar, Final

from pydantic import BaseModel, ConfigDict, Field

MAX_LIMIT: Final = 64
POLL_SECONDS: Final = 0.25


class LeaseCancelled(RuntimeError):  # noqa: N818 - control flow, not an error condition.
    """The waiter was cancelled before it obtained its lease; nothing was held."""


class LeaseDuplicateError(ValueError):
    """Another callback already holds or awaits this exact task's lease."""

    def __init__(self, owner: str) -> None:
        """Keep the owner available without including any model input."""
        self.owner: str = owner
        super().__init__("同一执行者不能重复申请租约")


class LeaseRequest(BaseModel):
    """What one worker needs: a project slot, an account slot and its write files."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="forbid")
    owner: str = Field(min_length=1, max_length=200)
    project: str = Field(min_length=1)
    account: str = Field(min_length=1, max_length=200)
    # Files this worker may write; empty with exclusive=False is a read-only worker.
    files: tuple[str, ...] = ()
    # A writer with no declared files may touch anything in its project.
    exclusive: bool = False
    project_limit: int = Field(default=1, ge=1, le=MAX_LIMIT)
    account_limit: int = Field(default=1, ge=1, le=MAX_LIMIT)
    model: str = Field(default="", max_length=1024)
    model_limit: int = Field(default=MAX_LIMIT, ge=1, le=MAX_LIMIT)


class LeaseState(BaseModel):
    """A read-only picture for the panel; waiters carry the reason they are held back."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="forbid")
    held: tuple[str, ...]
    waiting: tuple[tuple[str, str], ...]


def normalize_path(path: str) -> str:
    """Compare writes by a conservative form: slash-separated, case-folded, no dot segments."""
    cleaned = posixpath.normpath(path.strip().replace("\\", "/")).lstrip("/")
    return "" if cleaned == "." else cleaned.casefold()


def _overlap(left: str, right: str) -> bool:
    if not left or not right:
        return True
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


def conflicting_path(first: LeaseRequest, second: LeaseRequest) -> str:
    """The first overlapping write path of two workers in one project, or an empty string."""
    writes_first = first.exclusive or bool(first.files)
    writes_second = second.exclusive or bool(second.files)
    if not (writes_first and writes_second):
        return ""
    left_paths = ("",) if first.exclusive else first.files
    right_paths = ("",) if second.exclusive else second.files
    for left in left_paths:
        for right in right_paths:
            left_key = normalize_path(posixpath.join(first.project, left))
            right_key = normalize_path(posixpath.join(second.project, right))
            if _overlap(left_key, right_key):
                return "整个项目" if first.exclusive or second.exclusive else left
    return ""


def blocking_reason(
    request: LeaseRequest, active: list[LeaseRequest], reserved: list[LeaseRequest]
) -> str:
    """Why this worker cannot start now; reserved are earlier waiters that keep their place."""
    same_project = [item for item in active if item.project == request.project]
    if len(same_project) >= request.project_limit:
        return "项目并发已满"
    if sum(1 for item in active if item.account == request.account) >= request.account_limit:
        return "账户并发已满"
    if request.model and sum(item.model == request.model for item in active) >= request.model_limit:
        return "模型并行实例已满"
    for other in [*active, *reserved]:
        path = conflicting_path(request, other)
        if path:
            return "文件冲突：" + path
    return ""


@dataclass(frozen=True, slots=True)
class _Waiter:
    ticket: int
    request: LeaseRequest


class LeaseBroker:
    """First come first served, but a blocked worker never lets a conflicting later one pass."""

    def __init__(self) -> None:
        """Own mutable scheduling state behind one condition lock."""
        self._condition: threading.Condition = threading.Condition()
        self._held: dict[str, LeaseRequest] = {}
        self._waiting: list[_Waiter] = []
        self._tickets: int = 0

    def _evaluate(self) -> dict[int, str]:
        """Simulate the queue in arrival order; only waiters that pass may start."""
        active = list(self._held.values())
        reserved: list[LeaseRequest] = []
        verdicts: dict[int, str] = {}
        for waiter in sorted(self._waiting, key=lambda item: item.ticket):
            reason = blocking_reason(waiter.request, active, reserved)
            verdicts[waiter.ticket] = reason
            if reason:
                reserved.append(waiter.request)
            else:
                active.append(waiter.request)
        return verdicts

    def acquire(self, request: LeaseRequest, cancelled: Callable[[], bool]) -> None:
        """Block until the worker may start; cancellation leaves no trace behind."""
        with self._condition:
            if request.owner in self._held or any(
                item.request.owner == request.owner for item in self._waiting
            ):
                raise LeaseDuplicateError(request.owner)
            self._tickets += 1
            waiter = _Waiter(self._tickets, request)
            self._waiting.append(waiter)
            try:
                while True:
                    if cancelled():
                        raise LeaseCancelled(request.owner)
                    verdicts = self._evaluate()
                    if not verdicts[waiter.ticket]:
                        self._held[request.owner] = request
                        return
                    _ = self._condition.wait(POLL_SECONDS)
            finally:
                self._waiting.remove(waiter)
                self._condition.notify_all()

    def release(self, owner: str) -> None:
        """Idempotent: an unknown owner is ignored so cleanup can always run."""
        with self._condition:
            _ = self._held.pop(owner, None)
            self._condition.notify_all()

    def try_acquire(self, request: LeaseRequest) -> bool:
        """Admit a short recovery operation without blocking its request handler."""
        with self._condition:
            if request.owner in self._held or any(
                item.request.owner == request.owner for item in self._waiting
            ):
                raise LeaseDuplicateError(request.owner)
            self._tickets += 1
            waiter = _Waiter(self._tickets, request)
            self._waiting.append(waiter)
            try:
                if self._evaluate()[waiter.ticket]:
                    return False
                self._held[request.owner] = request
                return True
            finally:
                self._waiting.remove(waiter)
                self._condition.notify_all()

    def state(self) -> LeaseState:
        """Who holds a lease and why each waiter is waiting."""
        with self._condition:
            verdicts = self._evaluate()
            waiting = tuple(
                (item.request.owner, verdicts[item.ticket])
                for item in sorted(self._waiting, key=lambda entry: entry.ticket)
            )
            return LeaseState(held=tuple(sorted(self._held)), waiting=waiting)
