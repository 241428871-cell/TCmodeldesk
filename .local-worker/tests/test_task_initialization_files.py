"""File publication and admission failures retain original records and transaction boundaries."""

from __future__ import annotations

import os
import sys
from pathlib import Path
import stat
from typing import TYPE_CHECKING

import pytest

from model_desk_control import host_initialization_files as files
from model_desk_control import host_initialization_store as store
from model_desk_control.database import text_cursor
from model_desk_control.host_events import exists

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import model_registry as registry
import stage_store
import task_queue
from test_goal_action_receipts import GoalRun, apply, proposal
from test_goal_action_receipts import run as run
from test_task_initialization import fail_preparation, original_task
from test_task_queue import capture

if TYPE_CHECKING:
    from sqlite3 import Connection

    from model_desk_control.host_initialization_models import InitialDraft


def test_new_record_publication_syncs_its_parent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "record.json"
    synced: list[tuple[int, int]] = []
    real_fsync = os.fsync

    def observe(descriptor: int) -> None:
        identity = os.fstat(descriptor)
        if stat.S_ISDIR(identity.st_mode):
            synced.append((identity.st_dev, identity.st_ino))
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", observe)
    files.publish(tmp_path, Path(target.name), '{"saved":true}')
    parent = tmp_path.stat()
    assert (parent.st_dev, parent.st_ino) in synced
    assert target.read_text() == '{"saved":true}'


def test_existing_complete_record_retries_directory_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "record.json"
    attempts = 0
    sync = files.sync_directory

    def interrupt(directory: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            message = "fixture directory sync interrupted"
            raise OSError(message)
        sync(directory)

    monkeypatch.setattr(files, "sync_directory", interrupt)
    with pytest.raises(OSError, match="directory sync interrupted"):
        files.publish(tmp_path, Path(target.name), '{"saved":true}')
    assert target.read_text() == '{"saved":true}'
    files.publish(tmp_path, Path(target.name), '{"saved":true}')
    assert attempts == 2


def test_task_json_replace_syncs_file_and_new_parent_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "new" / "deep" / "task.json"
    synced_files = 0
    synced_directories: list[tuple[int, int]] = []
    real_fsync = os.fsync

    def observe(descriptor: int) -> None:
        nonlocal synced_files
        identity = os.fstat(descriptor)
        if stat.S_ISDIR(identity.st_mode):
            synced_directories.append((identity.st_dev, identity.st_ino))
        else:
            synced_files += 1
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", observe)
    registry.write_json(target, {"status": "recovery_pending"})
    assert registry.read_json(target) == {"status": "recovery_pending"}
    assert synced_files == 1
    for directory in (tmp_path, tmp_path / "new", tmp_path / "new/deep"):
        identity = directory.stat()
        assert (identity.st_dev, identity.st_ino) in synced_directories


def test_lost_task_file_publication_reuses_the_complete_original_prompt(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = capture(monkeypatch)
    link = os.link
    attempts: list[Path] = []

    def interrupt(source: str, target: Path, *, follow_symlinks: bool = True) -> None:
        if target.name == "task.json":
            attempts.append(target)
            if len(attempts) == 1:
                message = "Fixture interrupted before atomic task publication"
                raise OSError(message)
        link(source, target, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "link", interrupt)
    task_id = apply(run, proposal(run)).dispatched_task_ids[0]
    assert len(attempts) == 2
    assert len(set(attempts)) == 1
    assert task_queue.observed(registry.read_task(registry.task_dir(task_id))) is not None


def test_existing_conflicting_task_bytes_are_preserved(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    callbacks = capture(monkeypatch)
    publish = files.publish

    def conflict(project: Path, relative: Path, content: str) -> None:
        if relative.name == "task.json":
            _ = (project / relative).write_text("foreign-record")
        publish(project, relative, content)

    monkeypatch.setattr(files, "publish", conflict)
    actions = proposal(run)
    with pytest.raises(ValueError, match="未知"):
        _ = apply(run, actions)
    task_id = original_task(run)
    monkeypatch.setattr(files, "publish", publish)
    with pytest.raises(ValueError, match="existing_content_conflict"):
        _ = apply(run, actions)
    assert (registry.task_dir(task_id) / "task.json").read_text() == "foreign-record"
    assert task_queue.observed({"project": run.scope.project, "id": task_id}) is None
    assert callbacks == []


def test_initialization_manifest_and_stage_claim_roll_back_together(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    save = store.save

    def interrupt(database: Connection, draft: InitialDraft) -> None:
        save(database, draft)
        message = "Fixture interruption before original claim transaction commit"
        raise OSError(message)

    monkeypatch.setattr(store, "save", interrupt)
    with pytest.raises(ValueError, match="未知"):
        _ = apply(run, proposal(run))
    assert original_task(run) == ""
    with stage_store.connection(run.scope.project) as database:
        if exists(database, "goal_task_initializations"):
            assert (
                text_cursor(database, "SELECT task_id FROM goal_task_initializations").fetchall()
                == []
            )


def test_startup_retains_an_admitted_task_even_before_any_task_file_exists(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(registry, "prepare_task_dir", fail_preparation)
    with pytest.raises(ValueError, match="未知"):
        _ = apply(run, proposal(run))
    task_id = original_task(run)
    _ = stage_store.recover()
    with stage_store.connection(run.scope.project) as database:
        row = text_cursor(
            database, "SELECT status FROM stages WHERE active_task_id=?", (task_id,)
        ).fetchone()
    assert row is not None
    assert row[0] == "dispatched"
    assert not (Path(run.scope.project) / ".model-desk/runs" / task_id / "task.json").exists()
