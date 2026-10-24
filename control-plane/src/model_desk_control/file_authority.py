"""Human file authority and bounded AI-selected paths, independent of any brain host."""

from pathlib import Path, PurePosixPath
from typing import ClassVar, Final, Literal, assert_never

from pydantic import BaseModel, ConfigDict, Field

type FileScope = Literal["project", "selected"]
MAX_FILES: Final = 200
MAX_PATH: Final = 500
CONTROL_LIMIT: Final = 32
PROTECTED_COMPONENTS: Final = frozenset(
    {".model-desk", ".git", ".omo", ".codex", ".claude", ".aws"}
)
RUNTIME_DIRECTORIES: Final = frozenset(
    {
        "runs",
        "tasks",
        "runtime",
        "venv",
        "node_modules",
        "backups",
        "project-migration-backups",
        "dsh-home",
        "workflow-guides-runtime",
        "desktop-integrations",
    }
)
RUNTIME_FILES: Final = frozenset(
    {
        "models.json",
        "settings.json",
        "workflow.json",
        "workspace.json",
        "engineering-policy.json",
        "project-index.json",
        "project-bookmarks.json",
        "panel-runtime.json",
        "stages.db",
        "provider-accounts.json",
        "accounts.json",
        "ledger.json",
        "usage.json",
        "execution-endpoints.json",
        "workbuddy.json",
        "workbuddy-catalog.json",
        "workbuddy-access.json",
        "budget-ledger.json",
        "endpoint-access.json",
    }
)


class FileAuthorityError(ValueError):
    """A path violates the declared authority; no filesystem write has occurred."""

    def __init__(self, code: str, relative_path: str = "") -> None:
        """Retain a stable reason and the requested project-relative spelling."""
        self.code: str = code
        self.relative_path: str = relative_path
        super().__init__(code)


def clean_relative_files(values: tuple[str, ...]) -> tuple[str, ...]:
    """Normalize explicit finite names without expanding a directory or glob."""
    result: list[str] = []
    for value in values:
        path = PurePosixPath(value.replace("\\", "/"))
        text = str(path)
        if (
            not value.strip()
            or path.is_absolute()
            or ".." in path.parts
            or ":" in value
            or text == "."
            or "*" in text
            or "?" in text
            or len(text) > MAX_PATH
            or any(ord(char) < CONTROL_LIMIT for char in value)
        ):
            code = "invalid_relative_file"
            raise FileAuthorityError(code, value)
        if text not in result:
            result.append(text)
    if len(result) > MAX_FILES:
        code = "file_count_exceeded"
        raise FileAuthorityError(code)
    return tuple(result)


def _protected(path: PurePosixPath) -> bool:
    parts = tuple(part.casefold() for part in path.parts)
    if PROTECTED_COMPONENTS.intersection(parts):
        return True
    return _runtime_path(parts)


def _runtime_path(parts: tuple[str, ...]) -> bool:
    for index, part in enumerate(parts[:-1]):
        if part != ".local-worker":
            continue
        child = parts[index + 1]
        if (
            child in RUNTIME_FILES
            or child in RUNTIME_DIRECTORIES
            or child.endswith(".lock")
            or ".db" in child
        ):
            return True
        if (
            child == "agent-runtime"
            and index + 2 < len(parts)
            and parts[index + 2] in {"state", "node_modules"}
        ):
            return True
    return False


class FileAuthority(BaseModel):
    """Missing scope stays selected; callers explicitly choose the new-goal default."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="forbid")
    scope: FileScope = "selected"
    allowed_files: tuple[str, ...] = Field(default=(), max_length=MAX_FILES)

    def validate_paths(self, project: Path, values: tuple[str, ...]) -> tuple[str, ...]:
        """Check current real paths and preserve exact selected-file restrictions."""
        names = clean_relative_files(values)
        root = project.resolve(strict=True)
        for name in names:
            target = (root / name).resolve()
            if not target.is_relative_to(root):
                code = "file_outside_project"
                raise FileAuthorityError(code, name)
            actual = PurePosixPath(target.relative_to(root).as_posix())
            absolute = tuple(part.casefold() for part in target.parts)
            if (
                _protected(PurePosixPath(name))
                or _protected(actual)
                or (PROTECTED_COMPONENTS - {".omo"}).intersection(absolute)
                or _runtime_path(absolute)
            ):
                code = "protected_control_path"
                raise FileAuthorityError(code, name)
            if target.exists() and not target.is_file():
                code = "file_target_is_not_regular"
                raise FileAuthorityError(code, name)
        match self.scope:
            case "project":
                return names
            case "selected":
                selected = frozenset(clean_relative_files(self.allowed_files))
                for name in names:
                    if name not in selected:
                        code = "file_not_selected"
                        raise FileAuthorityError(code, name)
                return names
            case _:
                assert_never(self.scope)


def choose_file_scope(
    requested: FileScope | None, *, supplied_files: bool, previous: FileScope | None
) -> FileScope:
    """Preserve historical limits and criteria-only updates; new omitted scope is project."""
    if requested is not None:
        return requested
    if supplied_files:
        return "selected"
    return previous or "project"
