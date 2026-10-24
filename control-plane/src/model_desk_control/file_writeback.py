"""Versioned, conflict-aware writeback of explicitly selected disposable files.

Cooperating callers must hold their resource lease and live goal authority.
Per-file replacements are atomic; a batch is not a crash-atomic transaction.
"""

import hashlib
import os
import shutil
import stat
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile

from model_desk_control.host_flow import HostRecord
from model_desk_control.host_initialization_files import publish, sync_directory


class WritebackError(ValueError):
    """The proposal or current file identity cannot be safely written back."""


class WritebackConflictError(WritebackError):
    """An intervening edit must be reconciled without overwriting it."""

    names: tuple[str, ...]

    def __init__(self, names: tuple[str, ...]) -> None:
        """Expose relative conflicting names without embedding file contents."""
        self.names = names
        super().__init__("writeback_conflict: " + ", ".join(names))


class FileVersion(HostRecord):
    """Content and permission identity, including a previously absent file."""

    name: str
    digest: str | None
    mode: int | None


class WritebackPlan(HostRecord):
    """A persisted proposal remains available after its disposable copy is removed."""

    baseline: tuple[FileVersion, ...]
    proposed: tuple[FileVersion, ...]


def selected_path(root: Path, name: str) -> Path:
    """Reject linked components and paths outside the selected root."""
    relative = PurePosixPath(name)
    if not name or relative.is_absolute() or ".." in relative.parts or "\\" in name:
        raise WritebackError("writeback_invalid_path: " + name)
    target = root
    for part in relative.parts:
        target /= part
        if target.is_symlink():
            raise WritebackError("writeback_linked_path: " + name)
    if not target.resolve().is_relative_to(root.resolve()) or target == root:
        raise WritebackError("writeback_invalid_path: " + name)
    return target


def _version(path: Path, name: str) -> FileVersion:
    if not path.exists():
        return FileVersion(name=name, digest=None, mode=None)
    if not path.is_file():
        raise WritebackError("writeback_not_file: " + name)
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    return FileVersion(name=name, digest=digest, mode=stat.S_IMODE(path.stat().st_mode))


def snapshot(root: Path, names: tuple[str, ...]) -> tuple[FileVersion, ...]:
    """Capture versions before making a disposable copy or applying a proposal."""
    if len(names) != len(set(names)):
        reason = "writeback_duplicate_names"
        raise WritebackError(reason)
    return tuple(_version(selected_path(root, name), name) for name in names)


def require_baseline(root: Path, baseline: tuple[FileVersion, ...]) -> None:
    """A copy made during another write cannot become the model's baseline."""
    observed = snapshot(root, tuple(version.name for version in baseline))
    changed = tuple(old.name for old, new in zip(baseline, observed, strict=True) if old != new)
    if changed:
        raise WritebackConflictError(changed)


def preserve(
    source: Path, destination: Path, baseline: tuple[FileVersion, ...]
) -> WritebackPlan:
    """Retain candidate files and hashes inside the original task's capture directory."""
    destination.mkdir(parents=True, exist_ok=False)
    for version in baseline:
        candidate = selected_path(source, version.name)
        target = selected_path(destination, version.name)
        if candidate.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            _ = shutil.copy2(candidate, target)
    proposed = snapshot(destination, tuple(version.name for version in baseline))
    plan = WritebackPlan(baseline=baseline, proposed=proposed)
    publish(destination.parent, Path("writeback-plan.json"), plan.model_dump_json())
    return plan


def _replace(source: Path, target: Path, expected: FileVersion) -> None:
    missing_parents: list[Path] = []
    parent = target.parent
    while not parent.exists():
        missing_parents.append(parent)
        parent = parent.parent
    target.parent.mkdir(parents=True, exist_ok=True)
    for created in reversed(missing_parents):
        sync_directory(created.parent)
    with NamedTemporaryFile(dir=target.parent, prefix=".desk-writeback-", delete=False) as file:
        temporary = Path(file.name)
    try:
        _ = shutil.copy2(source, temporary)
        if _version(temporary, expected.name) != expected:
            raise WritebackError("writeback_proposal_changed: " + expected.name)
        with temporary.open("rb") as staged:
            os.fsync(staged.fileno())
        _ = temporary.replace(target)
        sync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)


def apply(
    plan: WritebackPlan, original: Path, candidates: Path,
    on_applied: Callable[[tuple[str, ...]], None],
) -> tuple[str, ...]:
    """Preflight the entire batch, then recheck and replace each changed file.

    Untouched proposals do not overwrite newer originals. Already applied exact
    results are idempotent. Uncooperative external editors can still race the
    final check; this does not claim filesystem compare-and-swap semantics.
    """
    missing = tuple(version.name for version in plan.proposed if version.digest is None)
    if missing:
        raise WritebackError("writeback_missing_proposal: " + ", ".join(missing))
    require_baseline(candidates, plan.proposed)
    pending: list[tuple[FileVersion, FileVersion]] = []
    conflicts: list[str] = []
    for old, new in zip(plan.baseline, plan.proposed, strict=True):
        if old == new:
            continue
        current = _version(selected_path(original, old.name), old.name)
        if current == new:
            continue
        if current != old:
            conflicts.append(old.name)
        pending.append((old, new))
    if conflicts:
        raise WritebackConflictError(tuple(conflicts))
    applied: tuple[str, ...] = ()
    for old, new in pending:
        target = selected_path(original, old.name)
        if _version(target, old.name) != old:
            raise WritebackConflictError((old.name,))
        _replace(selected_path(candidates, new.name), target, new)
        applied = (*applied, new.name)
        on_applied(applied)
    return applied
