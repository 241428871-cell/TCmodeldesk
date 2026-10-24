"""Pure native command mapping; building arguments performs no discovery or model work."""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import assert_never
from uuid import uuid4

from model_desk_control.claude_output import STREAM_ARGUMENTS

# Relative to the brain's working directory, which is always the owning project.
RECORD_EDIT_DENY = "Edit(./.model-desk/**)"


class NativeHost(StrEnum):
    """Host identity is never inferred from a model display name."""

    CLAUDE = "claude_cli"
    CODEX = "codex_cli"


class NativeMode(StrEnum):
    """New goals use native auto; historical read-only sessions retain their snapshot."""

    AUTO = "auto"
    READ_ONLY = "read_only"


@dataclass(frozen=True, slots=True)
class NativeCommandSpec:
    """Trusted host settings after the existing runtime-options validator has run."""

    host: NativeHost
    mode: NativeMode
    executable: str
    model: str
    project: Path
    output: Path
    schema: Path
    schema_json: str
    runtime_args: tuple[str, ...] = ()
    session_id: str = ""


@dataclass(frozen=True, slots=True)
class NativeLaunch:
    """Exact argv and expected session identity; no shell expression is involved."""

    arguments: tuple[str, ...]
    session_id: str


def _claude(spec: NativeCommandSpec) -> NativeLaunch:
    session = spec.session_id or str(uuid4())
    session_args = ("--resume", spec.session_id) if spec.session_id else ("--session-id", session)
    match spec.mode:
        case NativeMode.AUTO:
            # Native auto keeps file and terminal tools; edit tools never reach the records.
            permissions = (
                "--permission-mode",
                "auto",
                "--tools",
                "default",
                "--disallowedTools",
                RECORD_EDIT_DENY,
            )
        case NativeMode.READ_ONLY:
            permissions = ("--permission-mode", "plan", "--restricted", "--tools", "Read,Glob,Grep")
        case _:
            assert_never(spec.mode)
    return NativeLaunch(
        arguments=(
            spec.executable,
            "-p",
            "--model",
            spec.model,
            *STREAM_ARGUMENTS,
            "--permission-prompts",
            "none",
            "--safe-mode",
            *permissions,
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--settings",
            '{"disableAllHooks":true}',
            "--json-schema",
            spec.schema_json,
            *spec.runtime_args,
            *session_args,
        ),
        session_id=session,
    )


def codex_permission_arguments(mode: NativeMode) -> tuple[str, ...]:
    """Share native auto semantics between decision and execution adapters."""
    match mode:
        case NativeMode.AUTO:
            # Explicit configuration also works for exec resume, whose help does not
            # expose the --approve-for-me convenience flag on the installed release.
            return (
                "-c",
                'approval_policy="on-request"',
                "-c",
                'approvals_reviewer="auto_review"',
                "-c",
                'sandbox_mode="workspace-write"',
            )
        case NativeMode.READ_ONLY:
            return ("-c", 'approval_policy="never"', "-c", 'sandbox_mode="read-only"')
        case _:
            assert_never(mode)


def _codex(spec: NativeCommandSpec) -> NativeLaunch:
    permissions = codex_permission_arguments(spec.mode)
    prefix = (spec.executable, "exec", "resume") if spec.session_id else (spec.executable, "exec")
    ending = (spec.session_id, "-") if spec.session_id else ("--cd", str(spec.project), "-")
    return NativeLaunch(
        arguments=(
            *prefix,
            *spec.runtime_args,
            "--ignore-user-config",
            "--ignore-rules",
            "--json",
            "--skip-git-repo-check",
            "--model",
            spec.model,
            "--output-schema",
            str(spec.schema),
            "--output-last-message",
            str(spec.output),
            *permissions,
            "-c",
            "features.hooks=false",
            *ending,
        ),
        session_id=spec.session_id,
    )


def build_native_command(spec: NativeCommandSpec) -> NativeLaunch:
    """Map one explicit native host and mode without applying API-model defaults."""
    match spec.host:
        case NativeHost.CLAUDE:
            return _claude(spec)
        case NativeHost.CODEX:
            return _codex(spec)
        case _:
            assert_never(spec.host)
