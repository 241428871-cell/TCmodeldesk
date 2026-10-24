"""Resolve external CLIs without borrowing a desktop application's Codex binary."""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Final

CODEX_COMMAND_ENV: Final = 'MODEL_DESK_CODEX_CLI'


def _desktop_bundle(path: Path) -> bool:
    return '.app/Contents/' in str(path.resolve())


def codex_command() -> Path | None:
    """Honor an explicit path, then find a standalone command; never use app resources."""
    declared = os.environ.get(CODEX_COMMAND_ENV, '').strip()
    if declared:
        candidate = Path(declared).expanduser()
        return candidate if (candidate.is_absolute() and candidate.is_file()
                             and os.access(candidate, os.X_OK) and not _desktop_bundle(candidate)) else None
    found = shutil.which('codex')
    if found and not _desktop_bundle(Path(found)):
        return Path(found)
    # Desktop hosts can prepend their private resources to PATH. They must not
    # shadow an independent installation further along the user's PATH.
    candidates = [Path.home() / '.local/bin/codex',
                  *(Path(directory) / 'codex' for directory in os.get_exec_path())]
    return next((p for p in candidates if p.is_file() and os.access(p, os.X_OK)
                 and not _desktop_bundle(p)), None)


def executable(engine: str) -> Path | None:
    match engine:
        case 'codex_cli':
            return codex_command()
        case 'claude_cli':
            # Keep cswap's current executable and login environment. In
            # particular, never resolve and pin the target of a version symlink.
            found = shutil.which('claude')
            return Path(found) if found else None
        case _:
            raise ValueError('未知原生 CLI 执行端')
