# Narrow typed contract for the existing native executor entrypoint.

from pathlib import Path
from typing import NotRequired, TypedDict

class CliRuntimeError(ValueError): ...

class CliRuntime(TypedDict, total=False):
    effort: str
    fast: bool
    context_window: int | None
    autocompact_tokens: int | None

class NativeTask(TypedDict):
    engine: str
    kind: str
    project: str
    files: list[str]
    timeout: int
    cli_runtime: NotRequired[CliRuntime]
    test_command: NotRequired[str]
    goal_id: NotRequired[str]
    stage_id: NotRequired[str]
    authority_epoch: NotRequired[int]
    goal_contract_revision: NotRequired[int]
    automatic_goal: NotRequired[bool]

class NativeModel(TypedDict):
    provider: str
    model: str
    cli_runtime: NotRequired[CliRuntime]

def run_task(run: Path, state: NativeTask, model: NativeModel) -> None: ...
def main() -> int: ...
