"""Shared command receipts describe historical results, never live authorization."""

from hashlib import sha256
from sqlite3 import Connection
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from model_desk_control.database import StoreError, text_cursor


class Receipt(BaseModel):
    """Parse stored receipts before their response enters any domain logic."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="forbid")
    payload_hash: str
    response: str


def fingerprint(goal_id: str, payload: str) -> str:
    """Bind a namespaced command to its trusted goal and exact request body."""
    return sha256(f"{goal_id}\0{payload}".encode()).hexdigest()


def replay(database: Connection, request_id: str, payload_hash: str) -> str | None:
    """Return the original response; payload changes cannot reuse a request ID."""
    row = text_cursor(
        database,
        "SELECT payload_hash, response FROM receipts WHERE request_id = ?",
        (request_id,),
    ).fetchone()
    if row is None:
        return None
    receipt = Receipt(payload_hash=row[0], response=row[1])
    if receipt.payload_hash != payload_hash:
        code = "request_id_payload_conflict"
        raise StoreError(code)
    return receipt.response


def record(
    database: Connection, *, request_id: str, goal_id: str, payload_hash: str, response: str
) -> None:
    """Write a receipt in the same transaction as its domain mutation."""
    _ = database.execute(
        "INSERT INTO receipts VALUES (?, ?, ?, ?)",
        (request_id, goal_id, payload_hash, response),
    )
