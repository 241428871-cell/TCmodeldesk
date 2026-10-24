"""Reject unrelated repositories and roll back failed upgrades without data loss."""

from contextlib import closing
from pathlib import Path
from sqlite3 import OperationalError, connect

import pytest

from model_desk_control import database
from model_desk_control.control import Control
from model_desk_control.database import StoreError, text_cursor
from model_desk_control.plan_store import PlanStore
from model_desk_control.plans import Plan
from model_desk_control.schema import APPLICATION_ID, CORE, DOMAIN
from model_desk_control.store import ControlStore


def make_legacy(path: Path, *, versioned: bool) -> Control:
    state = Control(goal_id="legacy", due_at=17)
    with closing(connect(path)) as connection, connection:
        for statement in CORE:
            _ = connection.execute(statement)
        _ = connection.execute(
            "INSERT INTO controls VALUES (?, ?, ?, ?)",
            (state.goal_id, state.epoch, state.revision, state.model_dump_json()),
        )
        if versioned:
            _ = connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
            _ = connection.execute("PRAGMA user_version = 1")
    return state


def test_explicit_legacy_adoption_preserves_authority(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite"
    state = make_legacy(path, versioned=False)
    original = path.read_bytes()
    store = ControlStore(path)
    with pytest.raises(StoreError, match="legacy_adoption_required"):
        store.initialize()
    assert path.read_bytes() == original
    store.initialize(adopt_legacy=True)
    assert store.read("legacy") == state
    store.initialize()
    assert store.read("legacy") == state
    # Missing historical plans are not fabricated from an old graph checkpoint.
    with store.connection() as connection:
        assert text_cursor(connection, "SELECT document FROM plans").fetchall() == []


def test_recognized_version_upgrade_retains_goal(tmp_path: Path) -> None:
    path = tmp_path / "v1.sqlite"
    state = make_legacy(path, versioned=True)
    store = ControlStore(path)
    with pytest.raises(StoreError, match="database_version_not_current"):
        _ = store.read("legacy")
    store.initialize()
    assert store.read("legacy") == state


@pytest.mark.parametrize("future", [False, True])
def test_initialization_rejects_foreign_or_future_without_mutation(
    tmp_path: Path, *, future: bool
) -> None:
    path = tmp_path / "unrelated.sqlite"
    with closing(connect(path)) as connection, connection:
        _ = connection.execute("CREATE TABLE unrelated (secret TEXT)")
        _ = connection.execute("INSERT INTO unrelated VALUES ('untouched')")
        if future:
            _ = connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
            _ = connection.execute("PRAGMA user_version = 999")
    original = path.read_bytes()
    with pytest.raises(StoreError, match=r"too_new|schema_mismatch"):
        ControlStore(path).initialize(adopt_legacy=True)
    assert path.read_bytes() == original
    assert not path.with_suffix(".sqlite-wal").exists()


def test_current_version_with_altered_schema_is_rejected_on_read(tmp_path: Path) -> None:
    store = ControlStore(tmp_path / "modified.sqlite")
    store.initialize()
    with closing(connect(store.path)) as connection, connection:
        _ = connection.execute("ALTER TABLE plans ADD COLUMN injected TEXT")
    with pytest.raises(StoreError, match="database_schema_mismatch"):
        _ = store.read("absent")


def test_upgrade_failure_rolls_back_all_ddl_and_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "failed.sqlite"
    original_state = make_legacy(path, versioned=True)
    original = path.read_bytes()
    monkeypatch.setattr(
        database,
        "MIGRATIONS",
        (CORE, ("CREATE TABLE temporary_step (value TEXT)", "INVALID SQL")),
    )
    with pytest.raises(OperationalError, match="syntax error"):
        ControlStore(path).initialize()
    assert path.read_bytes() == original
    with closing(connect(path)) as connection:
        row = text_cursor(connection, "SELECT document FROM controls").fetchone()
        assert row is not None
        assert Control.model_validate_json(row[0]) == original_state
        assert (
            text_cursor(
                connection, "SELECT name FROM sqlite_schema WHERE name = 'temporary_step'"
            ).fetchone()
            is None
        )


def test_version_two_upgrade_preserves_plan_and_does_not_fabricate_operations(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v2.sqlite"
    state = Control(goal_id="old-goal", due_at=100)
    plan = Plan(revision=3)
    with closing(connect(path)) as connection, connection:
        for statement in (*CORE, *DOMAIN):
            _ = connection.execute(statement)
        _ = connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        _ = connection.execute("PRAGMA user_version = 2")
        _ = connection.execute(
            "INSERT INTO controls VALUES (?, ?, ?, ?)",
            (state.goal_id, state.epoch, state.revision, state.model_dump_json()),
        )
        _ = connection.execute(
            "INSERT INTO plans VALUES (?, ?, ?)",
            (state.goal_id, plan.revision, plan.model_dump_json()),
        )
    controls = ControlStore(path)
    controls.initialize()
    assert controls.read(state.goal_id) == state
    assert PlanStore(controls).read(state.goal_id) == plan
    with controls.connection() as connection:
        assert text_cursor(connection, "SELECT operation_id FROM planning_rounds").fetchall() == []
