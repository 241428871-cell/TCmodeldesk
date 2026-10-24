"""Bounded regular-file observations for human review, never a cached permission grant."""

import os
from hashlib import sha256
from pathlib import Path
from typing import Final, Literal

from model_desk_control.file_authority import FileAuthority, FileAuthorityError
from model_desk_control.host_flow import HostRecord
from model_desk_control.native_output import (
    NativeReadError,
    file_identity,
    owned_path,
    regular_file,
)

MAX_FILE_BYTES: Final = 20 * 1024 * 1024
TOTAL_BYTES: Final = 32 * 1024 * 1024
CHUNK_BYTES: Final = 64 * 1024


class ReviewScopeError(ValueError):
    """Evidence that cannot be fully observed cannot authorize a human disposition."""

    def __init__(self, code: str) -> None:
        """Keep source content and absolute paths out of interface failures."""
        self.code: str = code
        super().__init__(code)


class Artifact(HostRecord):
    """Missing output is an explicit fact, distinct from an empty regular file."""

    path: str
    status: Literal["missing", "present"]
    digest: str
    size: int


class ReviewArtifacts(HostRecord):
    """Paths are sorted so unrelated stage ordering cannot change a subject."""

    files: tuple[Artifact, ...]

    @property
    def digest(self) -> str:
        """Bind the review to file bytes and presence, rather than modification time alone."""
        return sha256(self.model_dump_json().encode()).hexdigest()


def _read(project: Path, name: str, remaining: int) -> Artifact:
    target = owned_path(project, Path(name))
    try:
        with regular_file(target) as stream:
            before = os.fstat(stream.fileno())
            if before.st_size > min(MAX_FILE_BYTES, remaining):
                code = "review_artifact_budget"
                raise ReviewScopeError(code)
            digest = sha256()
            count = 0
            while chunk := stream.read(min(CHUNK_BYTES, remaining - count + 1)):
                count += len(chunk)
                if count > min(MAX_FILE_BYTES, remaining):
                    code = "review_artifact_budget"
                    raise ReviewScopeError(code)
                digest.update(chunk)
            if (
                file_identity(before) != file_identity(os.fstat(stream.fileno()))
                or file_identity(before) != file_identity(target.stat())
                or count != before.st_size
            ):
                code = "review_artifact_changed"
                raise ReviewScopeError(code)
    except FileNotFoundError:
        return Artifact(path=name, status="missing", digest="", size=0)
    return Artifact(path=name, status="present", digest=digest.hexdigest(), size=count)


def observe(project: Path, paths: tuple[str, ...]) -> ReviewArtifacts:
    """Read at most 32 MiB per subject; no metadata-only cache stands in for real bytes."""
    try:
        names = FileAuthority(scope="project").validate_paths(project, tuple(sorted(set(paths))))
        remaining = TOTAL_BYTES
        evidence: list[Artifact] = []
        for name in names:
            file = _read(project, name, remaining)
            remaining -= file.size
            evidence.append(file)
        return ReviewArtifacts(files=tuple(evidence))
    except (FileAuthorityError, NativeReadError, OSError) as error:
        code = "review_artifact_unreadable"
        raise ReviewScopeError(code) from error
