"""Current lease identity is checked inside the caller's domain transaction."""

from sqlite3 import Connection
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from model_desk_control.authority import read_control
from model_desk_control.control import UserIntent
from model_desk_control.database import text_cursor


class ContinuationLease(BaseModel):
    """Exact owner and expiry identify a lease generation, not a native launch receipt."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="forbid")
    operation_id: str = Field(min_length=1)
    goal_id: str = Field(min_length=1)
    epoch: int = Field(ge=0)
    owner: str = Field(min_length=1)
    expires_at: int = Field(ge=0)


def lease_current(database: Connection, lease: ContinuationLease, now: int) -> bool:
    """A renewed generation immediately invalidates the previous expiry token."""
    control = read_control(database, lease.goal_id)
    if (
        control.closed
        or control.intent is not UserIntent.RUN
        or control.resource_hold
        or control.epoch != lease.epoch
        or now < 0
        or now >= lease.expires_at
    ):
        return False
    row = text_cursor(
        database,
        """SELECT owner FROM outbox WHERE operation_id = ? AND goal_id = ? AND epoch = ?
           AND state = 'leased' AND lease_until = ?""",
        (lease.operation_id, lease.goal_id, str(lease.epoch), str(lease.expires_at)),
    ).fetchone()
    return row is not None and row[0] == lease.owner
