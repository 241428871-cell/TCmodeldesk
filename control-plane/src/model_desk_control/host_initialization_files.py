"""Publish complete initial task files without replacing existing content."""

import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from model_desk_control.host_initialization_models import InitializationError
from model_desk_control.native_output import owned_path, read_metadata


def sync_directory(directory: Path) -> None:
    """Persist directory entries on the supported POSIX hosts."""
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish(project: Path, relative: Path, content: str) -> None:
    """A temporary file can be abandoned; the original target is linked only when complete."""
    target = owned_path(project, relative)
    if target.exists():
        if read_metadata(target) != content:
            code = "initialization_existing_content_conflict"
            raise InitializationError(code)
        sync_directory(target.parent)
        return
    with NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=target.parent, prefix=".initialization-", delete=True
    ) as pending:
        _ = pending.write(content)
        pending.flush()
        os.fsync(pending.fileno())
        _ = owned_path(project, relative)
        try:
            os.link(pending.name, target, follow_symlinks=False)
            sync_directory(target.parent)
        except FileExistsError:
            if read_metadata(owned_path(project, relative)) != content:
                code = "initialization_publish_conflict"
                raise InitializationError(code) from None
            sync_directory(target.parent)
