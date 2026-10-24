"""Reserve and capture a native launch under the existing live goal authority."""

import subprocess
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, TextIO

import continuation_clock
import governance_context
import model_registry as registry
import platform_runtime
from pydantic import BaseModel, ConfigDict, JsonValue

type Record = dict[str, JsonValue]


class GoalAuthority(BaseModel):
    """Only current goal identity is needed while waiting for a native child."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="ignore")
    project: str | None = None
    goal_id: str | None = None
    authority_epoch: int | None = None


@contextmanager
def fence(state: Record) -> Generator[None]:
    """Bound a communicate slice by current authority without rehashing project artifacts."""
    scope = GoalAuthority.model_validate(state)
    if scope.project and scope.goal_id:
        with continuation_clock.authority_guard(
            scope.project, scope.goal_id, scope.authority_epoch
        ):
            yield
    else:
        yield


class LaunchCancelledError(ValueError):
    """A cancelled file record cannot authorize a native account call."""

    def __init__(self) -> None:
        """Keep the reason independent of model output."""
        super().__init__("任务已取消，未启动原生模型")


@dataclass(frozen=True, slots=True)
class NativeLaunch:
    """The caller owns the output streams and disposable project directory."""

    argv: tuple[str, ...]
    directory: Path
    stdout: TextIO
    stderr: TextIO
    capture_id: str


@contextmanager
def spawn(run: Path, state: Record, launch: NativeLaunch) -> Generator[subprocess.Popen[str]]:
    """Release the goal lock before model work; an armed unknown launch stays visible."""
    process: subprocess.Popen[str] | None = None
    with ExitStack() as resources:
        try:
            with governance_context.launch_authority(state):
                if (run / "cancelled.flag").exists():
                    raise LaunchCancelledError
                _ = registry.patch_task(run, native_launch_state="armed")
                try:
                    process = resources.enter_context(
                        subprocess.Popen(  # noqa: S603 - argv comes from native command builders.
                            launch.argv,
                            cwd=launch.directory,
                            stdin=subprocess.PIPE,
                            stdout=launch.stdout,
                            stderr=launch.stderr,
                            text=True,
                            encoding="utf-8",
                            errors="replace",
                            **platform_runtime.process_options(),
                        )
                    )
                except OSError:
                    _ = registry.patch_task(run, native_launch_state="not_started")
                    raise
                _ = registry.patch_task(
                    run,
                    process_pid=process.pid,
                    process_birth=platform_runtime.process_birth(process.pid),
                    native_output_capture=1,
                    native_capture_id=launch.capture_id,
                    native_launch_state="started",
                    cli_stage=str(launch.directory),
                )
            yield process
        finally:
            if process is not None and process.poll() is None:
                platform_runtime.stop_tree(process.pid)
