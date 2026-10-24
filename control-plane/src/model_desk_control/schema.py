"""Versioned domain schema; graph checkpoints live in a separate repository."""

from typing import Final

APPLICATION_ID: Final = 0x4D444350
SCHEMA_VERSION: Final = 3

CORE: Final = (
    """CREATE TABLE IF NOT EXISTS controls (
     goal_id TEXT PRIMARY KEY, epoch INTEGER NOT NULL, revision INTEGER NOT NULL,
     document TEXT NOT NULL CHECK(json_valid(document))
    )""",
    """CREATE TABLE IF NOT EXISTS receipts (
     request_id TEXT PRIMARY KEY, goal_id TEXT NOT NULL,
     payload_hash TEXT NOT NULL, response TEXT NOT NULL CHECK(json_valid(response))
    )""",
    """CREATE TABLE IF NOT EXISTS outbox (
     operation_id TEXT PRIMARY KEY, goal_id TEXT NOT NULL, epoch INTEGER NOT NULL,
     document TEXT NOT NULL CHECK(json_valid(document)), state TEXT NOT NULL,
     owner TEXT NOT NULL DEFAULT '', lease_until INTEGER
    )""",
)

DOMAIN: Final = (
    """CREATE TABLE plans (
     goal_id TEXT NOT NULL REFERENCES controls(goal_id), revision INTEGER NOT NULL,
     document TEXT NOT NULL CHECK(json_valid(document)),
     PRIMARY KEY (goal_id, revision)
    )""",
    """CREATE TABLE task_states (
     goal_id TEXT NOT NULL REFERENCES controls(goal_id), task_id TEXT NOT NULL,
     document TEXT NOT NULL CHECK(json_valid(document)),
     PRIMARY KEY (goal_id, task_id)
    )""",
    """CREATE TABLE decisions (
     decision_id TEXT PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES controls(goal_id),
     subject_hash TEXT NOT NULL, revision INTEGER NOT NULL,
     state TEXT NOT NULL CHECK(state IN ('pending', 'answered', 'consumed', 'invalidated')),
     document TEXT NOT NULL CHECK(json_valid(document)),
     UNIQUE (goal_id, subject_hash)
    )""",
)

ROUNDS: Final = (
    """CREATE TABLE planning_rounds (
     operation_id TEXT PRIMARY KEY REFERENCES outbox(operation_id),
     goal_id TEXT NOT NULL REFERENCES controls(goal_id),
     document TEXT NOT NULL CHECK(json_valid(document))
    )""",
    """CREATE TABLE round_feedback (
     operation_id TEXT NOT NULL REFERENCES planning_rounds(operation_id),
     receipt_id TEXT NOT NULL, sequence INTEGER NOT NULL,
     document TEXT NOT NULL CHECK(json_valid(document)),
     PRIMARY KEY (operation_id, receipt_id), UNIQUE (operation_id, sequence)
    )""",
)

MIGRATIONS: Final = (CORE, DOMAIN, ROUNDS)


def statements_at(version: int) -> tuple[str, ...]:
    """Return the exact schema expected at a supported revision."""
    return tuple(statement for migration in MIGRATIONS[:version] for statement in migration)


def normalized(statement: str) -> str:
    """Normalize only formatting in our fixed DDL, preserving all constraints."""
    formatted = " ".join(statement.rstrip("; \n").split())
    return formatted.replace("CREATE TABLE IF NOT EXISTS ", "CREATE TABLE ")
