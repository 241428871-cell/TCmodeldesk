"""Pure continuation transitions; no process, scheduler or provider side effects."""

from enum import StrEnum
from typing import Annotated, ClassVar, Final, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_INTERVAL: Final = 1800
Nonnegative = Annotated[int, Field(ge=0)]
Positive = Annotated[int, Field(gt=0)]


class ErrorCode(StrEnum):
    """Stable error codes shared by controller adapters and future HTTP routes."""

    CLOSED_HAS_ACTIVE_WORK = "closed_has_active_work"
    RUNNING_REQUIRES_CLOCK = "running_requires_clock"
    CLOSED = "closed"
    STALE_AUTHORITY_OR_REVISION = "stale_authority_or_revision"
    NOT_ELIGIBLE = "not_eligible"
    RESUME_NOT_AUTHORIZED_OR_WORKER_ACTIVE = "resume_not_authorized_or_worker_active"
    INTERVAL_CANNOT_SHORTEN = "interval_cannot_shorten"
    CLOSURE_OBLIGATIONS_INCOMPLETE = "closure_obligations_incomplete"
    PLAN_REVISION_CONFLICT = "plan_revision_conflict"
    DUPLICATE_TASK_IDENTITY = "duplicate_task_identity"
    CANNOT_REPLACE_UNKNOWN_OR_ACTIVE_TASK = "cannot_replace_unknown_or_active_task"
    DEPENDENCY_CYCLE = "dependency_cycle"
    UNKNOWN_OR_SUPERSEDED_DEPENDENCY = "unknown_or_superseded_dependency"


class ContractError(ValueError):
    """A rejected transition with a stable machine-readable reason."""

    def __init__(self, code: ErrorCode) -> None:
        """Retain the typed reason separately from its readable value."""
        self.code: ErrorCode = code
        super().__init__(code)


class UserIntent(StrEnum):
    """User authority is independent of resource availability."""

    RUN = "run"
    PAUSE = "user_pause"
    STOP = "stop"


class Control(BaseModel):
    """Immutable validated snapshot; callers persist it transactionally."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="forbid")

    goal_id: Annotated[str, Field(min_length=1)]
    epoch: Nonnegative = 0
    revision: Nonnegative = 0
    intent: UserIntent = UserIntent.RUN
    interval: Positive = DEFAULT_INTERVAL
    due_at: Nonnegative | None
    resource_hold: bool = False
    busy: bool = False
    goal_complete: bool = False
    saved: bool = False
    analyzed: bool = False
    accepted: bool = False
    cleaned: bool = False
    closed: bool = False

    @model_validator(mode="after")
    def coherent_clock(self) -> Self:
        """Reject records that could erase an active clock or revive a closed one."""
        if self.closed and (self.due_at is not None or self.busy):
            raise ContractError(ErrorCode.CLOSED_HAS_ACTIVE_WORK)
        if not self.closed and self.due_at is None and self.intent is UserIntent.RUN:
            raise ContractError(ErrorCode.RUNNING_REQUIRES_CLOCK)
        return self


def require_current(state: Control, *, epoch: int, revision: int) -> None:
    """Fence delayed events and concurrent writers before any transition."""
    if state.closed:
        raise ContractError(ErrorCode.CLOSED)
    if state.epoch != epoch or state.revision != revision:
        raise ContractError(ErrorCode.STALE_AUTHORITY_OR_REVISION)


def claim_tick(state: Control, *, epoch: int, revision: int, now: int) -> Control:
    """Claim one due continuation; the persistence layer must compare-and-swap."""
    require_current(state, epoch=epoch, revision=revision)
    if (
        state.intent is not UserIntent.RUN
        or state.resource_hold
        or state.busy
        or state.due_at is None
        or now < state.due_at
    ):
        raise ContractError(ErrorCode.NOT_ELIGIBLE)
    return state.model_copy(
        update={"busy": True, "revision": state.revision + 1, "due_at": now + state.interval}
    )


def stop(state: Control) -> Control:
    """Revoke future authority without pretending an in-flight worker exited."""
    if state.closed:
        raise ContractError(ErrorCode.CLOSED)
    return state.model_copy(
        update={
            "intent": UserIntent.STOP,
            "epoch": state.epoch + 1,
            "revision": state.revision + 1,
            "due_at": None,
        }
    )


def pause(state: Control) -> Control:
    """User pause revokes old dispatch authority but preserves the due time."""
    if state.closed:
        raise ContractError(ErrorCode.CLOSED)
    return state.model_copy(
        update={
            "intent": UserIntent.PAUSE,
            "epoch": state.epoch + 1,
            "revision": state.revision + 1,
        }
    )


def resume(state: Control, *, now: int, explicit_user_command: bool) -> Control:
    """Only a new user control command can restore paused or stopped authority."""
    if state.closed:
        raise ContractError(ErrorCode.CLOSED)
    if not explicit_user_command or state.busy:
        raise ContractError(ErrorCode.RESUME_NOT_AUTHORIZED_OR_WORKER_ACTIVE)
    return state.model_copy(
        update={
            "intent": UserIntent.RUN,
            "epoch": state.epoch + 1,
            "revision": state.revision + 1,
            "due_at": max(state.due_at or 0, now),
        }
    )


def set_resource_hold(state: Control, *, held: bool) -> Control:
    """Resource observations never change user intent, authority or due time."""
    if state.closed:
        raise ContractError(ErrorCode.CLOSED)
    return state.model_copy(update={"resource_hold": held, "revision": state.revision + 1})


def backoff(state: Control, *, interval: int, now: int) -> Control:
    """Increase the existing interval without creating a competing clock."""
    if state.closed or interval < state.interval:
        raise ContractError(ErrorCode.INTERVAL_CANNOT_SHORTEN)
    due = max(state.due_at, now + interval) if state.due_at is not None else None
    return state.model_copy(
        update={"interval": interval, "due_at": due, "revision": state.revision + 1}
    )


def close(state: Control) -> Control:
    """Normal closure requires all obligations and observed worker completion."""
    if (
        state.closed
        or state.intent is not UserIntent.RUN
        or state.busy
        or not all(
            (state.goal_complete, state.saved, state.analyzed, state.accepted, state.cleaned)
        )
    ):
        raise ContractError(ErrorCode.CLOSURE_OBLIGATIONS_INCOMPLETE)
    return state.model_copy(update={"closed": True, "due_at": None, "revision": state.revision + 1})
