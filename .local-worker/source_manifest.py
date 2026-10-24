"""Audit or explicitly rebuild the release source manifest from Git's index."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Final, TypedDict, cast, final
from uuid import uuid4

from pydantic import JsonValue, TypeAdapter

ROOT: Final = Path(__file__).resolve().parent.parent
MANIFEST_NAME: Final = "SOURCE-MANIFEST.json"
GIT_NOT_FOUND: Final = "git_not_found"
LINKED_OR_EXTERNAL: Final = "linked_or_external_path"
NOT_REGULAR: Final = "tracked_path_not_regular"
NOT_OBJECT: Final = "manifest_not_object"
METADATA_INCOMPLETE: Final = "manifest_metadata_incomplete"
UNTRACKED_FILES: Final = "untracked_release_files"
INVALID_DATE: Final = "invalid_release_date"
JSON_OBJECT: Final = TypeAdapter(dict[str, JsonValue])


class SourceEntry(TypedDict):
    """One regular tracked file and its exact working-tree digest."""

    path: str
    sha256: str
    bytes: int


class Manifest(TypedDict):
    """Portable release manifest schema."""

    project: str
    license: str
    release_date: str
    scope: str
    files: list[SourceEntry]
    repository: str
    copyright: str


@final
class ManifestError(ValueError):
    """The source tree cannot produce a complete, safe release manifest."""

    def __init__(self, code: str, detail: str = "") -> None:
        """Keep a stable machine code and an optional non-sensitive detail."""
        self.code = code
        super().__init__(code + (f": {detail}" if detail else ""))


def _git_paths(root: Path, *arguments: str) -> list[str]:
    git = shutil.which("git")
    if git is None:
        raise ManifestError(GIT_NOT_FOUND)
    result = subprocess.run(
        [git, "-C", str(root), *arguments, "-z"],
        check=True,
        capture_output=True,
    )
    return sorted(
        item.decode("utf-8") for item in result.stdout.split(b"\0") if item
    )


def tracked_paths(root: Path = ROOT) -> list[str]:
    """Return the exact Git-index release set, excluding the self-referential manifest."""
    return [path for path in _git_paths(root, "ls-files", "--cached") if path != MANIFEST_NAME]


def untracked_paths(root: Path = ROOT) -> list[str]:
    """Return non-ignored files that a release review has not added to Git yet."""
    return _git_paths(root, "ls-files", "--others", "--exclude-standard")


def entries(root: Path = ROOT) -> list[SourceEntry]:
    """Hash regular tracked files without following a repository link outside the tree."""
    result: list[SourceEntry] = []
    resolved_root = root.resolve()
    for relative in tracked_paths(root):
        path = root / relative
        if path.is_symlink() or not path.resolve().is_relative_to(resolved_root):
            raise ManifestError(LINKED_OR_EXTERNAL, relative)
        if not path.is_file():
            raise ManifestError(NOT_REGULAR, relative)
        content = path.read_bytes()
        result.append(
            {"path": relative, "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
        )
    return result


def _json_object(path: Path) -> dict[str, object]:
    try:
        value = JSON_OBJECT.validate_json(path.read_bytes())
    except ValueError as error:
        raise ManifestError(NOT_OBJECT) from error
    return cast("dict[str, object]", value)


def _metadata(path: Path) -> Manifest:
    if not path.is_file():
        return {
            "project": "多agent协作",
            "license": "MIT",
            "release_date": datetime.now(UTC).date().isoformat(),
            "scope": (
                "Git-tracked source files; excludes this manifest itself and ignored runtime data."
            ),
            "files": [],
            "repository": "https://github.com/Gambler134201/multi-agent-collaboration",
            "copyright": "Copyright (c) 2026 Gambler134201",
        }
    value = _json_object(path)
    required = ("project", "license", "release_date", "scope", "repository", "copyright")
    if any(not isinstance(value.get(key), str) or not value[key] for key in required):
        raise ManifestError(METADATA_INCOMPLETE)
    return {
        "project": cast("str", value["project"]),
        "license": cast("str", value["license"]),
        "release_date": cast("str", value["release_date"]),
        "scope": cast("str", value["scope"]),
        "files": [],
        "repository": cast("str", value["repository"]),
        "copyright": cast("str", value["copyright"]),
    }


def proposed(root: Path = ROOT, *, release_date: str | None = None) -> Manifest:
    """Build deterministic manifest content from current tracked working-tree bytes."""
    target = root / MANIFEST_NAME
    result = _metadata(target)
    if release_date is not None:
        _ = date.fromisoformat(release_date)
        result["release_date"] = release_date
    result["files"] = entries(root)
    return result


def audit(root: Path = ROOT) -> dict[str, object]:
    """Compare the saved manifest with current source without mutating anything."""
    target = root / MANIFEST_NAME
    missing = not target.is_file()
    current: object = None if missing else _json_object(target)
    expected = proposed(root)
    pending = untracked_paths(root)
    return {
        "ready": not missing and current == expected and not pending,
        "manifest_exists": not missing,
        "manifest_matches_index": not missing and current == expected,
        "tracked_files": len(expected["files"]),
        "untracked_files": len(pending),
        "untracked_paths": pending,
    }


def write(root: Path = ROOT, *, release_date: str | None = None) -> Path:
    """Atomically publish a reviewed manifest; untracked source makes omission unsafe."""
    pending = untracked_paths(root)
    if pending:
        raise ManifestError(UNTRACKED_FILES, str(len(pending)))
    target = root / MANIFEST_NAME
    today = datetime.now(UTC).date().isoformat()
    value = proposed(root, release_date=release_date or today)
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            _ = stream.write("\n")
            stream.flush()
            _ = os.fsync(stream.fileno())
        _ = temporary.replace(target)
        descriptor = os.open(root, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            _ = os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def main(arguments: list[str] | None = None) -> int:
    """Run the read-only audit by default; writing always requires ``--write``."""
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    _ = action.add_argument("--check", action="store_true", help="只读核对 (默认)")
    _ = action.add_argument("--write", action="store_true", help="显式原子重建最终清单")
    _ = parser.add_argument("--release-date", help="写入 YYYY-MM-DD；默认今天")
    parsed = cast("dict[str, object]", vars(parser.parse_args(arguments)))
    write_requested = parsed.get("write") is True
    release = parsed.get("release_date")
    if release is not None and not isinstance(release, str):
        raise ManifestError(INVALID_DATE)
    try:
        if write_requested:
            path = write(release_date=release)
            _ = sys.stdout.write(
                json.dumps({"written": str(path), "files": len(entries())}, ensure_ascii=False)
                + "\n"
            )
            return 0
        result = audit()
        _ = sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return 0 if result["ready"] else 1
    except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        message = json.dumps({"ready": False, "error": str(error)}, ensure_ascii=False)
        _ = sys.stderr.write(message + "\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
