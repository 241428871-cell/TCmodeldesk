"""File-version boundaries for cooperating writers, without any model process."""

import os
import stat
from pathlib import Path
from typing import Literal, assert_never

import pytest

from model_desk_control.file_writeback import (
    WritebackConflictError,
    WritebackError,
    WritebackPlan,
    apply,
    preserve,
    require_baseline,
    snapshot,
)


def test_disjoint_stale_copies_merge_without_reverting_each_other(tmp_path: Path) -> None:
    original = tmp_path / "original"
    original.mkdir()
    for name in ("a", "b"):
        _ = (original / name).write_text("base")
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    _ = (first / "a").write_text("first")
    _ = (second / "b").write_text("second")
    baseline = snapshot(original, ("a", "b"))
    first_plan = preserve(first, tmp_path / "capture1/files", baseline[:1])
    second_plan = preserve(second, tmp_path / "capture2/files", baseline[1:])
    receipts: list[tuple[str, ...]] = []
    assert apply(first_plan, original, tmp_path / "capture1/files", receipts.append) == ("a",)
    assert apply(second_plan, original, tmp_path / "capture2/files", receipts.append) == ("b",)
    assert (original / "a").read_text() == "first"
    assert (original / "b").read_text() == "second"
    assert apply(first_plan, original, tmp_path / "capture1/files", receipts.append) == ()
    assert receipts == [("a",), ("b",)]


@pytest.mark.parametrize("change", ["create", "edit", "delete", "mode"])
def test_changed_original_is_preserved(
    tmp_path: Path, change: Literal["create", "edit", "delete", "mode"]
) -> None:
    original = tmp_path / "original"
    source = tmp_path / "source"
    original.mkdir()
    source.mkdir()
    target = original / "a"
    if change != "create":
        _ = target.write_text("base")
    baseline = snapshot(original, ("a",))
    _ = (source / "a").write_text("proposal")
    candidates = tmp_path / "capture/files"
    plan = preserve(source, candidates, baseline)
    match change:
        case "create" | "edit":
            _ = target.write_text("human")
        case "delete":
            target.unlink()
        case "mode":
            target.chmod(0o700)
        case _:
            assert_never(change)
    observed = snapshot(original, ("a",))
    receipts: list[tuple[str, ...]] = []
    with pytest.raises(WritebackConflictError):
        _ = apply(plan, original, candidates, receipts.append)
    assert snapshot(original, ("a",)) == observed
    assert receipts == []


def test_unchanged_proposal_does_not_undo_external_edit(tmp_path: Path) -> None:
    original = tmp_path / "original"
    original.mkdir()
    _ = (original / "a").write_text("base")
    plan = preserve(original, tmp_path / "capture/files", snapshot(original, ("a",)))
    _ = (original / "a").write_text("human")
    receipts: list[tuple[str, ...]] = []
    assert apply(plan, original, tmp_path / "capture/files", receipts.append) == ()
    assert (original / "a").read_text() == "human"


def test_missing_candidate_blocks_whole_batch(tmp_path: Path) -> None:
    original = tmp_path / "original"
    source = tmp_path / "source"
    original.mkdir()
    source.mkdir()
    _ = (source / "a").write_text("proposal")
    plan = preserve(source, tmp_path / "capture/files", snapshot(original, ("a", "b")))
    receipts: list[tuple[str, ...]] = []
    with pytest.raises(WritebackError, match="writeback_missing_proposal"):
        _ = apply(plan, original, tmp_path / "capture/files", receipts.append)
    assert not (original / "a").exists()
    assert (tmp_path / "capture/files/a").read_text() == "proposal"


def test_replaced_parent_alias_and_copy_drift_are_refused(tmp_path: Path) -> None:
    original = tmp_path / "original"
    original.mkdir()
    _ = (original / "a").write_text("base")
    baseline = snapshot(original, ("a",))
    _ = (original / "a").write_text("intervening")
    with pytest.raises(WritebackConflictError):
        require_baseline(original, baseline)
    (original / "alias").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(WritebackError, match="writeback_linked_path"):
        _ = snapshot(original, ("alias/a",))


def test_tampered_saved_proposal_is_not_applied(tmp_path: Path) -> None:
    original = tmp_path / "original"
    original.mkdir()
    _ = (original / "a").write_text("base")
    baseline = snapshot(original, ("a",))
    _ = (original / "a").write_text("proposal")
    candidates = tmp_path / "capture/files"
    plan = preserve(original, candidates, baseline)
    _ = (original / "a").write_text("base")
    _ = (candidates / "a").write_text("tampered")
    receipts: list[tuple[str, ...]] = []
    with pytest.raises(WritebackConflictError):
        _ = apply(plan, original, candidates, receipts.append)
    assert (original / "a").read_text() == "base"


def test_partial_batch_reloads_plan_and_applies_only_remaining_files(tmp_path: Path) -> None:
    original = tmp_path / "original"
    original.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    for name in ("a", "b"):
        _ = (original / name).write_text("base")
        _ = (source / name).write_text("proposal-" + name)
    candidates = tmp_path / "capture/files"
    plan = preserve(source, candidates, snapshot(original, ("a", "b")))

    def interrupt(_applied: tuple[str, ...]) -> None:
        reason = "fixture process ended after first file replacement"
        raise OSError(reason)

    with pytest.raises(OSError, match="process ended"):
        _ = apply(plan, original, candidates, interrupt)
    assert (original / "a").read_text() == "proposal-a"
    assert (original / "b").read_text() == "base"
    timestamp = (original / "a").stat().st_mtime_ns
    content = (candidates.parent / "writeback-plan.json").read_bytes()
    loaded = WritebackPlan.model_validate_json(content)
    receipts: list[tuple[str, ...]] = []
    assert apply(loaded, original, candidates, receipts.append) == ("b",)
    assert (original / "a").stat().st_mtime_ns == timestamp
    assert (original / "b").read_text() == "proposal-b"
    assert receipts == [("b",)]


def test_replacement_syncs_new_parent_entries_and_final_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = tmp_path / "original"
    source = tmp_path / "source"
    original.mkdir()
    source.mkdir()
    nested = "new/deep/result.txt"
    candidate = source / nested
    candidate.parent.mkdir(parents=True)
    _ = candidate.write_text("durable proposal")
    candidates = tmp_path / "capture/files"
    plan = preserve(source, candidates, snapshot(original, (nested,)))
    synced_directories: list[tuple[int, int]] = []
    real_fsync = os.fsync

    def observe(descriptor: int) -> None:
        mode = os.fstat(descriptor).st_mode
        if stat.S_ISDIR(mode):
            identity = os.fstat(descriptor)
            synced_directories.append((identity.st_dev, identity.st_ino))
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", observe)
    assert apply(plan, original, candidates, lambda _files: None) == (nested,)
    assert (original / nested).read_text() == "durable proposal"
    for directory in (original, original / "new", original / "new/deep"):
        identity = directory.stat()
        assert (identity.st_dev, identity.st_ino) in synced_directories
