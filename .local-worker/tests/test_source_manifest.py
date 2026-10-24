"""Release manifest is deterministic, index-based and refuses silent omissions."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import TypeAdapter

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".local-worker"))
import source_manifest


def git(root: Path, *arguments: str) -> None:
    executable = shutil.which("git")
    assert executable is not None
    _ = subprocess.run(
        [executable, "-C", str(root), *arguments], check=True, capture_output=True
    )


def repository(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    root.mkdir()
    git(root, "init", "--quiet")
    _ = (root / "a.txt").write_text("alpha\n", encoding="utf-8")
    nested = root / "目录"
    nested.mkdir()
    _ = (nested / "b.txt").write_bytes(b"beta\n")
    git(root, "add", "a.txt", "目录/b.txt")
    return root


def test_manifest_uses_sorted_index_files_and_excludes_itself(tmp_path: Path) -> None:
    root = repository(tmp_path)
    initial = source_manifest.proposed(root, release_date="2026-10-23")
    _ = (root / source_manifest.MANIFEST_NAME).write_text(
        json.dumps(initial, ensure_ascii=False), encoding="utf-8"
    )
    git(root, "add", source_manifest.MANIFEST_NAME)

    value = source_manifest.proposed(root, release_date="2026-10-23")

    assert [row["path"] for row in value["files"]] == ["a.txt", "目录/b.txt"]
    assert value["files"][0] == {
        "path": "a.txt",
        "sha256": hashlib.sha256(b"alpha\n").hexdigest(),
        "bytes": 6,
    }
    assert value["release_date"] == "2026-10-23"


def test_audit_and_write_refuse_untracked_release_files(tmp_path: Path) -> None:
    root = repository(tmp_path)
    current = source_manifest.proposed(root, release_date="2026-10-23")
    _ = (root / source_manifest.MANIFEST_NAME).write_text(
        json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    git(root, "add", source_manifest.MANIFEST_NAME)
    assert source_manifest.audit(root)["ready"] is True

    _ = (root / "new.py").write_text("value = 1\n", encoding="utf-8")
    result = source_manifest.audit(root)
    assert result["ready"] is False
    assert result["untracked_paths"] == ["new.py"]
    with pytest.raises(source_manifest.ManifestError, match="untracked_release_files"):
        _ = source_manifest.write(root, release_date="2026-10-23")


def test_explicit_write_is_complete_after_the_manifest_joins_the_index(tmp_path: Path) -> None:
    root = repository(tmp_path)

    target = source_manifest.write(root, release_date="2026-10-23")

    saved = TypeAdapter(source_manifest.Manifest).validate_json(target.read_bytes())
    assert saved["release_date"] == "2026-10-23"
    assert [row["path"] for row in saved["files"]] == ["a.txt", "目录/b.txt"]
    git(root, "add", source_manifest.MANIFEST_NAME)
    assert source_manifest.audit(root)["ready"] is True


def test_tracked_symlink_is_never_followed(tmp_path: Path) -> None:
    root = repository(tmp_path)
    outside = tmp_path / "outside.txt"
    _ = outside.write_text("private", encoding="utf-8")
    (root / "linked.txt").symlink_to(outside)
    git(root, "add", "linked.txt")

    with pytest.raises(source_manifest.ManifestError, match="linked_or_external_path"):
        _ = source_manifest.entries(root)
