"""Apply a real macOS write boundary to Claude native processes."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

DIRECTORY_INVALID: Final = "native_sandbox_directory_invalid"
PATH_INVALID: Final = "native_sandbox_path_invalid"
SANDBOX_UNAVAILABLE: Final = "native_sandbox_unavailable"


class NativeWriteSandboxError(ValueError):
    """The native process cannot be started with its promised write boundary."""


@dataclass(frozen=True, slots=True)
class ProtectedLaunch:
    """Exact native argv plus the capability label persisted with the run."""

    argv: tuple[str, ...]
    mode: Literal[
        "codex-native-sandbox",
        "macos-seatbelt-workspace-write",
        "disposable-project-copy",
    ]


@dataclass(frozen=True, slots=True)
class ProtectRequest:
    """Inputs whose paths define one native process write boundary."""

    engine: str
    argv: tuple[str, ...]
    workspace: Path
    scratch: Path


def _directory(path: Path, *, create: bool = False) -> Path:
    if create:
        path.mkdir(mode=0o700, parents=False, exist_ok=True)
    if path.is_symlink() or not path.is_dir() or not path.is_absolute():
        raise NativeWriteSandboxError(DIRECTORY_INVALID)
    resolved = path.resolve(strict=True)
    if "\n" in str(resolved) or "\x00" in str(resolved):
        raise NativeWriteSandboxError(PATH_INVALID)
    return resolved


def _literal(path: Path) -> str:
    value = str(path).replace("\\", "\\\\").replace('"', '\\"')
    return f'(subpath "{value}")'


def protect(request: ProtectRequest) -> ProtectedLaunch:
    """Fence Claude writes on macOS; preserve explicit limits on other hosts."""
    if request.engine == "codex_cli":
        return ProtectedLaunch(argv=request.argv, mode="codex-native-sandbox")
    if request.engine != "claude_cli" or sys.platform != "darwin":
        return ProtectedLaunch(argv=request.argv, mode="disposable-project-copy")

    sandbox = Path("/usr/bin/sandbox-exec")
    if sandbox.is_symlink() or not sandbox.is_file():
        raise NativeWriteSandboxError(SANDBOX_UNAVAILABLE)
    allowed = (
        _directory(request.workspace),
        _directory(Path.home() / ".claude"),
        _directory(request.scratch, create=True),
        Path("/private/tmp") / f"claude-{os.getuid()}",
        Path("/private/tmp/cc-socks"),
    )
    profile = "".join(
        (
            "(version 1)",
            "(allow default)",
            "(deny file-write*)",
            *(f"(allow file-write* {_literal(path)})" for path in allowed),
            '(allow file-write* (literal "/dev/null"))',
        ),
    )
    protected = (
        "/usr/bin/env",
        f"TMPDIR={allowed[2]}",
        "DISABLE_AUTOUPDATER=1",
        str(sandbox),
        "-p",
        profile,
        *request.argv,
    )
    return ProtectedLaunch(argv=protected, mode="macos-seatbelt-workspace-write")
