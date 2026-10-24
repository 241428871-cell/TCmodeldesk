"""Review evidence depends on actual bounded files, including changes during observation."""

import os
from pathlib import Path
from typing import Literal, assert_never

import pytest

from model_desk_control import review_artifacts
from model_desk_control.native_output import NativeReadError, read_metadata
from model_desk_control.review_artifacts import ReviewScopeError, observe


def test_missing_empty_and_changed_same_size_file_are_distinct(tmp_path: Path) -> None:
    path = tmp_path / "answer.txt"
    missing = observe(tmp_path, (path.name,)).digest
    _ = path.write_text("")
    empty = observe(tmp_path, (path.name,)).digest
    _ = path.write_text("first")
    before = path.stat()
    original = observe(tmp_path, (path.name,)).digest
    _ = path.write_text("other")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert len({missing, empty, original, observe(tmp_path, (path.name,)).digest}) == 4


def test_order_and_repeated_file_names_do_not_change_subject(tmp_path: Path) -> None:
    _ = (tmp_path / "a").write_text("a")
    _ = (tmp_path / "b").write_text("b")
    assert observe(tmp_path, ("b", "a", "a")) == observe(tmp_path, ("a", "b"))


@pytest.mark.parametrize(("per_file", "total"), [(3, 10), (10, 6)])
def test_file_and_total_read_budgets_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, per_file: int, total: int
) -> None:
    for name in ("a", "b"):
        _ = (tmp_path / name).write_text("four")
    monkeypatch.setattr(review_artifacts, "MAX_FILE_BYTES", per_file)
    monkeypatch.setattr(review_artifacts, "TOTAL_BYTES", total)
    with pytest.raises(ReviewScopeError, match="review_artifact_budget"):
        _ = observe(tmp_path, ("a", "b"))


@pytest.mark.parametrize("name", ["../foreign", ".model-desk/task.json", ".git/config"])
def test_review_cannot_read_foreign_or_protected_files(tmp_path: Path, name: str) -> None:
    with pytest.raises(ReviewScopeError, match="review_artifact_unreadable"):
        _ = observe(tmp_path, (name,))


def test_even_internal_symlink_cannot_stand_in_for_observed_file(tmp_path: Path) -> None:
    _ = (tmp_path / "actual").write_text("evidence")
    (tmp_path / "alias").symlink_to(tmp_path / "actual")
    with pytest.raises(ReviewScopeError, match="review_artifact_unreadable"):
        _ = observe(tmp_path, ("alias",))


@pytest.mark.skipif(os.name == "nt", reason="POSIX named pipe")
def test_nonregular_file_is_rejected_without_blocking(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "pipe")
    with pytest.raises(ReviewScopeError, match="review_artifact_unreadable"):
        _ = observe(tmp_path, ("pipe",))


@pytest.mark.parametrize("reader", ["artifact", "metadata"])
@pytest.mark.parametrize("change", ["replace", "modify"])
def test_replacement_or_in_place_change_during_read_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reader: Literal["artifact", "metadata"],
    change: Literal["replace", "modify"],
) -> None:
    path = tmp_path / "evidence"
    _ = path.write_text('{"value":"before"}')
    original_fstat = os.fstat
    calls: list[int] = []

    def change_after_read(descriptor: int) -> os.stat_result:
        calls.append(descriptor)
        if len(calls) == 3:
            match change:
                case "replace":
                    replacement = tmp_path / "replacement"
                    _ = replacement.write_text('{"value":"after"}')
                    _ = replacement.replace(path)
                case "modify":
                    _ = path.write_text('{"value":"changed after read"}')
                case _:
                    assert_never(change)
        return original_fstat(descriptor)

    monkeypatch.setattr(os, "fstat", change_after_read)
    match reader:
        case "artifact":
            with pytest.raises(ReviewScopeError, match="review_artifact_changed"):
                _ = observe(tmp_path, (path.name,))
        case "metadata":
            with pytest.raises(NativeReadError, match="metadata_changed_during_read"):
                _ = read_metadata(path)
        case _:
            assert_never(reader)


def test_metadata_budget_cannot_be_bypassed_with_valid_json(tmp_path: Path) -> None:
    path = tmp_path / "task.json"
    _ = path.write_text('{"result":"large"}')
    with pytest.raises(NativeReadError, match="metadata_budget_exceeded"):
        _ = read_metadata(path, byte_limit=4)
