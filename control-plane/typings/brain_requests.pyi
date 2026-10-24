# The existing request journal's public seam; real temporary-store tests cover recovery.

from collections.abc import Callable
from typing import TypedDict

from pydantic import JsonValue

type Record = dict[str, JsonValue]

class JournalResult(TypedDict):
    receipt: Record

class RequestError(ValueError): ...
class RequestRejected(RequestError): ...

class UnknownOutcome(RequestError):
    reference: str
    kind: str
    request_id: str
    def __init__(
        self, message: str, reference: str, kind: str = "", request_id: str = ""
    ) -> None: ...

def canonical_payload(payload: Record) -> str: ...
def fingerprint(kind: str, payload: Record) -> str: ...
def run_once(
    project: str,
    owner: str,
    kind: str,
    request_id: str,
    payload: Record,
    effect: Callable[[], Record],
    recover: Callable[[], Record | None] | None = None,
    reject_on: tuple[type[ValueError], ...] = (),
) -> JournalResult: ...
