# Narrow existing-host transaction boundary; real integration tests exercise its implementation.

from contextlib import AbstractContextManager
from sqlite3 import Connection
from typing import TypedDict

from pydantic import JsonValue

class ClockError(ValueError): ...
class RequirementsChangedError(ClockError): ...

class IntentRecord(TypedDict, total=False):
    id: str
    goal_id: str
    authority_epoch: int
    owner_epoch: int
    owner_id: str
    status: str
    lease_until: str
    requirements_revision: int

class ClockSnapshot(TypedDict, total=False):
    project: str
    goal_id: str
    enabled: bool
    user_intent: str
    authority_epoch: int
    owner_epoch: int
    requirements_revision: int
    active_intent_id: str
    active_intent: IntentRecord | None
    hold: dict[str, JsonValue]
    closed: bool
    status: str

def mutation(project: str, goal_id: str) -> AbstractContextManager[Connection]: ...
def write(database: Connection, clock: dict[str, JsonValue]) -> None: ...
def write_intent(database: Connection, record: dict[str, JsonValue]) -> None: ...
def readonly(project: str) -> AbstractContextManager[Connection | None]: ...
def locked(project: str, goal_id: str) -> AbstractContextManager[None]: ...
def read(database: Connection, goal_id: str) -> dict[str, JsonValue] | None: ...
def active(
    database: Connection, clock: dict[str, JsonValue]
) -> dict[str, JsonValue] | None: ...
def iso(value: float | None = None) -> str: ...
