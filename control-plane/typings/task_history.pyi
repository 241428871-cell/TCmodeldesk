# The permanent-deletion seam used by tests; real temporary stores exercise the implementation.

import os as os
from collections.abc import Callable
from pathlib import Path
from typing import TypedDict

from pydantic import JsonValue

class DeletionPreview(TypedDict):
    token: str
    added_ids: list[str]
    records: dict[str, int]
    goal_brain_rounds: int

class DeletionResult(TypedDict):
    deleted_tasks: int
    cleanup_pending: bool

def preview(view: dict[str, str], ids: list[str]) -> DeletionPreview: ...
def delete(
    view: dict[str, str], token: str, idle_guard: Callable[[], None] | None = None
) -> DeletionResult: ...
def recover() -> None: ...
def staging_root() -> Path: ...
def _purge_threads(project: str, manifest: dict[str, JsonValue]) -> None: ...
