from contextlib import AbstractContextManager
from pathlib import Path
from subprocess import Popen

from pydantic import JsonValue

# The existing public exception is retained by both host adapters.
class DispatchBlocked(ValueError): ...  # noqa: N818

def guard(
    project: str,
    goal_id: str,
    stage_id: str = "",
    authority_epoch: int | None = None,
    files: list[str] | None = None,
    kind: str | None = None,
    contract_revision: int | None = None,
    require_contract_revision: bool = False,
    automatic_goal: bool = False,
    *,
    task_id: str = "",
) -> dict[str, JsonValue]: ...

def launch_authority(state: dict[str, JsonValue]) -> AbstractContextManager[None]: ...
def fenced_popen(
    run: Path, state: dict[str, JsonValue], argv: list[str], **options: object
) -> Popen[str]: ...
