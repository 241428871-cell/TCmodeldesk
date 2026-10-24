"""Bind shared file authority to legacy goal records without model or service work."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import control_bootstrap

if TYPE_CHECKING:
    from model_desk_control.file_authority import FileScope


def resolve_scope(
    requested: FileScope | None, *, supplied_files: bool, previous: FileScope | None
) -> FileScope:
    """Only an explicit human configuration call chooses a new-goal default."""
    control_bootstrap.bind_source()
    from model_desk_control.file_authority import choose_file_scope

    return choose_file_scope(requested, supplied_files=supplied_files, previous=previous)


def validate_files(
    project: str, scope: FileScope, allowed_files: list[str], files: list[str]
) -> list[str]:
    """Apply one common project/selected contract before planning and dispatch."""
    control_bootstrap.bind_source()
    from plan_contracts import PlanError

    from model_desk_control.file_authority import FileAuthority, FileAuthorityError

    try:
        authority = FileAuthority(scope=scope, allowed_files=tuple(allowed_files))
        return list(authority.validate_paths(Path(project), tuple(files)))
    except FileAuthorityError as error:
        raise PlanError(error.code, "目标文件范围拒绝请求: " + error.relative_path) from error
