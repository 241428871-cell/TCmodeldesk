"""Purge inventories are read-only, protect live records and refuse changed evidence."""

import json
import sqlite3
from collections.abc import Generator

import pytest

from model_desk_control.database import text_cursor
from model_desk_control.host_purge import PurgeError, PurgeScope, inventory, purge

SCOPE = PurgeScope(project="/project", goal_ids=("goal",), task_ids=("task",))


@pytest.fixture
def database() -> Generator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    try:
        yield connection
    finally:
        connection.close()


def seed(database: sqlite3.Connection, *, intent_status: str = "completed") -> None:
    schemas = {
        "continuation_clocks": ("goal_id", "data"),
        "continuation_intents": ("id", "goal_id", "status", "data"),
        "goal_requirements": ("id", "goal_id", "version", "kind", "status", "data"),
        "goal_task_initializations": (
            "task_id",
            "goal_id",
            "round_id",
            "request_id",
            "status",
            "data",
        ),
        "brain_requests": ("request_id", "owner", "kind", "state", "task_id"),
    }
    for table, columns in schemas.items():
        _ = database.execute("CREATE TABLE " + table + "(" + ",".join(columns) + ")")
    _ = database.execute(
        "INSERT INTO continuation_clocks VALUES ('goal', ?)",
        (json.dumps({"active_intent_id": "", "obligations": []}),),
    )
    _ = database.execute(
        "INSERT INTO continuation_intents VALUES ('intent','goal',?,?)",
        (intent_status, json.dumps({"authority_epoch": 1, "owner_id": "driver"})),
    )
    _ = database.execute(
        "INSERT INTO goal_requirements VALUES ('requirement','goal',1,'requirement','applied','{}')"
    )
    _ = database.execute(
        "INSERT INTO goal_task_initializations VALUES ('task','goal','intent','r','ready','{}')"
    )
    _ = database.execute(
        "INSERT INTO brain_requests VALUES ('r','other','dispatch','completed','task')"
    )


def tables(database: sqlite3.Connection) -> list[str]:
    rows = text_cursor(database, "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return sorted(row[0] for row in rows)


def test_inventory_of_an_untouched_store_creates_nothing(database: sqlite3.Connection) -> None:
    found = inventory(database, SCOPE)
    assert found.blockers == ()
    assert tables(database) == []


def test_active_round_is_a_blocker_and_purge_refuses(database: sqlite3.Connection) -> None:
    seed(database, intent_status="unknown")
    found = inventory(database, SCOPE)
    assert found.blockers
    _ = database.execute("BEGIN")
    with pytest.raises(PurgeError, match="purge_protected_records"):
        _ = purge(database, SCOPE, found)
    database.rollback()


def test_purge_requires_the_previewed_inventory(database: sqlite3.Connection) -> None:
    seed(database)
    found = inventory(database, SCOPE)
    with pytest.raises(PurgeError, match="purge_transaction_required"):
        _ = purge(database, SCOPE, found)
    _ = database.execute("UPDATE goal_task_initializations SET status='pending'")
    _ = database.execute("BEGIN")
    with pytest.raises(PurgeError, match="purge_inventory_changed"):
        _ = purge(database, SCOPE, found)
    database.rollback()


def test_purge_removes_goal_and_task_rows_only(database: sqlite3.Connection) -> None:
    seed(database)
    _ = database.execute(
        "INSERT INTO continuation_clocks VALUES ('other', ?)", (json.dumps({"obligations": []}),)
    )
    _ = database.execute(
        "INSERT INTO goal_requirements VALUES ('other-r','other',1,'requirement','applied','{}')"
    )
    found = inventory(database, SCOPE)
    assert found.goal_threads[0].startswith("native-goal-v1:")
    _ = database.execute("BEGIN")
    _ = purge(database, SCOPE, found)
    database.commit()
    remaining = text_cursor(database, "SELECT goal_id FROM continuation_clocks").fetchall()
    assert [row[0] for row in remaining] == ["other"]
    requirements = text_cursor(database, "SELECT goal_id FROM goal_requirements").fetchall()
    assert [row[0] for row in requirements] == ["other"]
    for table in ("continuation_intents", "goal_task_initializations", "brain_requests"):
        statement = "SELECT CAST(COUNT(*) AS TEXT) FROM " + table  # noqa: S608 - constant.
        assert text_cursor(database, statement).fetchall()[0][0] == "0"
