"""Goal-action identities and atomic receipts in the workbench's existing request table."""

from enum import StrEnum
from hashlib import sha256
from sqlite3 import Connection
from typing import ClassVar, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from model_desk_control.database import text_cursor
from model_desk_control.host_flow import HostRef

MAX_RECEIPT_BYTES: Final = 2 * 1024 * 1024


class EffectKind(StrEnum):
    """Reuse the existing non-atomic request kinds and name local transactional effects."""

    REVIEW = "review"
    DISPATCH = "dispatch"
    REWORK = "rework"
    HANDOVER = "goal_handover"
    PLAN = "goal_plan"
    QUESTION = "goal_question"
    OBLIGATION = "goal_obligation"
    COMPLETE = "goal_complete"
    BATCH = "goal_batch"


ATOMIC_KINDS: Final = frozenset(
    {
        EffectKind.PLAN,
        EffectKind.QUESTION,
        EffectKind.OBLIGATION,
        EffectKind.COMPLETE,
        EffectKind.BATCH,
    }
)


class EffectError(ValueError):
    """A receipt conflicts with its original action or cannot prove completion."""

    def __init__(self, code: str) -> None:
        """Retain a safe machine reason without native output or credentials."""
        self.code: str = code
        super().__init__(code)


class EffectRequest(BaseModel):
    """Host-generated identity; model actions cannot provide these fields."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="forbid")
    scope: HostRef
    owner: str = Field(pattern=r"^goal_[a-f0-9]{64}$")
    request_id: str = Field(pattern=r"^[a-z_]+_[a-f0-9]{40}$", max_length=80)
    kind: EffectKind
    payload_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class CommitReceipt(BaseModel):
    """Compact domain identity, not a copied mutable plan or clock snapshot."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="forbid")
    outcome: Literal["committed"]
    subject_id: str = Field(min_length=1, max_length=300)
    revision: int | None = Field(default=None, ge=0)


def effect_request(
    scope: HostRef, key: str, kind: EffectKind, canonical_payload: str
) -> EffectRequest:
    """Bind one action position to the immutable original round and exact payload."""
    return EffectRequest(
        scope=scope,
        owner="goal_" + sha256(scope.model_dump_json().encode()).hexdigest(),
        request_id=kind.value + "_" + sha256(key.encode()).hexdigest()[:40],
        kind=kind,
        payload_digest=sha256((kind.value + "\n" + canonical_payload).encode()).hexdigest(),
    )


def _receipt(raw: str) -> CommitReceipt:
    """Parse bounded receipt JSON before a saved record can stand in for an effect."""
    if len(raw.encode()) > MAX_RECEIPT_BYTES:
        code = "effect_receipt_size_exceeded"
        raise EffectError(code)
    try:
        return CommitReceipt.model_validate_json(raw)
    except ValidationError as error:
        code = "effect_receipt_invalid"
        raise EffectError(code) from error


def atomic_receipt(database: Connection, request: EffectRequest) -> CommitReceipt | None:
    """Read inside the domain transaction; non-atomic reservations cannot be retried here."""
    if request.kind not in ATOMIC_KINDS:
        code = "effect_requires_external_reconciliation"
        raise EffectError(code)
    row = text_cursor(
        database,
        """SELECT kind,payload_digest,state,receipt FROM brain_requests
           WHERE owner=? AND request_id=?""",
        (request.owner, request.request_id),
    ).fetchone()
    if row is None:
        return None
    if row[0] != request.kind.value or row[1] != request.payload_digest:
        code = "effect_payload_conflict"
        raise EffectError(code)
    if row[2] != "completed":
        code = "atomic_effect_has_uncommitted_reservation"
        raise EffectError(code)
    return _receipt(row[3])


def save_atomic_receipt(
    database: Connection, request: EffectRequest, receipt: CommitReceipt, recorded_at: str
) -> None:
    """Commit alongside local domain writes; the caller owns commit or rollback."""
    previous = atomic_receipt(database, request)
    if previous is not None:
        if previous != receipt:
            code = "effect_completed_receipt_conflict"
            raise EffectError(code)
        return
    _ = database.execute(
        """INSERT INTO brain_requests
           (request_id,owner,kind,payload_digest,state,task_id,receipt,detail,created_at,updated_at)
           VALUES (?,?,?,?,'completed','',?,'',?,?)""",
        (
            request.request_id,
            request.owner,
            request.kind.value,
            request.payload_digest,
            receipt.model_dump_json(),
            recorded_at,
            recorded_at,
        ),
    )
