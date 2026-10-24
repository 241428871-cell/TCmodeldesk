"""Adapt native goal actions to the existing per-project request journal."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar

import brain_requests
import continuation_store
import control_bootstrap
from pydantic import BaseModel, ConfigDict, JsonValue

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from sqlite3 import Connection

    from model_desk_control.host_effects import CommitReceipt, EffectRequest
    from model_desk_control.host_flow import HostRef

type Record = dict[str, JsonValue]


class ExternalReceipt(BaseModel):
    """Completion of an external action must identify its original task or explicit skip."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="ignore")
    task_id: str
    skipped: bool = False


@dataclass(frozen=True, slots=True)
class ExternalCall:
    """One external effect and its attributable recovery/refusal contract."""

    execute: Callable[[], Record]
    recover: Callable[[], Record | None]
    reject_on: tuple[type[ValueError], ...] = ()


def prepare(scope: HostRef | None, key: str, kind: str, payload: Record) -> EffectRequest | None:
    """Only the controller supplies round identity; old explicit calls retain their behavior."""
    if scope is None:
        return None
    control_bootstrap.bind_source()
    from model_desk_control.host_effects import EffectKind, effect_request

    return effect_request(scope, key, EffectKind(kind), brain_requests.canonical_payload(payload))


def verify(
    request: EffectRequest | None,
    project: str,
    goal_id: str,
    kind: str,
    payload: Record,
) -> None:
    """An internal receipt cannot be reused for a different domain call or project."""
    if request is None:
        return
    control_bootstrap.bind_source()
    from model_desk_control.host_effects import EffectError

    if (
        request.scope.project != project
        or request.scope.goal_id != goal_id
        or request.kind.value != kind
        or request.payload_digest != brain_requests.fingerprint(kind, payload)
    ):
        code = "effect_domain_scope_or_payload_mismatch"
        raise EffectError(code)


@contextmanager
def authority(request: EffectRequest | None) -> Generator[None]:
    """Local transactional effects retain the same current STOP/epoch fence."""
    if request is None:
        yield
        return
    import continuation_clock

    with continuation_clock.authority_guard(
        request.scope.project,
        request.scope.goal_id,
        request.scope.epoch,
    ):
        yield


def existing(database: Connection, request: EffectRequest | None) -> CommitReceipt | None:
    """Read a compact atomic receipt in the same transaction as its domain command."""
    if request is None:
        return None
    control_bootstrap.bind_source()
    from model_desk_control.host_effects import atomic_receipt

    return atomic_receipt(database, request)


def save(
    database: Connection,
    request: EffectRequest | None,
    subject_id: str,
    revision: int | None = None,
) -> None:
    """No separate commit: a rollback must remove both domain change and receipt."""
    if request is None:
        return
    control_bootstrap.bind_source()
    from model_desk_control.host_effects import CommitReceipt, save_atomic_receipt

    save_atomic_receipt(
        database,
        request,
        CommitReceipt(outcome="committed", subject_id=subject_id, revision=revision),
        datetime.now(UTC).isoformat(),
    )


def committed_revision(request: EffectRequest | None) -> int | None:
    """Read the original atomic plan generation, never an unrelated later revision."""
    if request is None:
        return None
    with continuation_store.mutation(request.scope.project, request.scope.goal_id) as database:
        receipt = existing(database, request)
        return None if receipt is None else receipt.revision


def external(
    project: str,
    request: EffectRequest | None,
    payload: Record,
    call: ExternalCall,
) -> Record:
    """Known original effects return their receipt; unknown effects are never reissued."""
    if request is None:
        return call.execute()
    verify(request, project, request.scope.goal_id, request.kind.value, payload)
    row = brain_requests.run_once(
        project,
        request.owner,
        request.kind.value,
        request.request_id,
        payload,
        call.execute,
        call.recover,
        reject_on=call.reject_on,
    )
    observed = ExternalReceipt.model_validate(row["receipt"])
    if not observed.task_id and (not observed.skipped or request.kind.value != "dispatch"):
        control_bootstrap.bind_source()
        from model_desk_control.host_effects import EffectError

        code = "effect_receipt_missing_task_identity"
        raise EffectError(code)
    return row["receipt"]


def bind_batch(scope: HostRef | None, payload: Record) -> None:
    """Persist immutable inputs, not an assertion that the batch's effects completed."""
    if scope is None:
        return
    request = prepare(scope, "batch", "goal_batch", payload)
    with continuation_store.mutation(scope.project, scope.goal_id) as database:
        _ = existing(database, request)
        save(database, request, scope.goal_id)
