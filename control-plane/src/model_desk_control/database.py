"""Explicit, version-checked SQLite access with transactional schema upgrades."""

from collections.abc import Generator, Sequence
from contextlib import closing, contextmanager
from pathlib import Path
from sqlite3 import Connection, connect
from typing import Protocol

from model_desk_control.schema import (
    APPLICATION_ID,
    MIGRATIONS,
    SCHEMA_VERSION,
    normalized,
    statements_at,
)


class StoreError(ValueError):
    """A database command rejection without SQL, secrets or provider details."""

    def __init__(self, code: str) -> None:
        """Retain a stable reason for HTTP and host adapters."""
        self.code: str = code
        super().__init__(code)


class TextCursor(Protocol):
    """Typed boundary for statements selecting only schema-owned TEXT columns."""

    def fetchone(self) -> Sequence[str] | None:
        """Return text columns with either a tuple or sqlite3.Row factory."""
        ...

    def fetchall(self) -> list[Sequence[str]]:
        """Return text columns for all selected rows."""
        ...


def text_cursor(
    database: Connection, statement: str, parameters: tuple[str | int, ...] = ()
) -> TextCursor:
    """Keep the untyped stdlib cursor outside the domain boundary."""
    return database.execute(statement, parameters)


def _header(database: Connection) -> tuple[int, int]:
    row = text_cursor(
        database,
        """SELECT CAST(application_id AS TEXT), CAST(user_version AS TEXT)
           FROM pragma_application_id, pragma_user_version""",
    ).fetchone()
    if row is None:
        code = "database_header_missing"
        raise StoreError(code)
    return int(row[0]), int(row[1])


def _check_schema(database: Connection, version: int) -> None:
    rows = text_cursor(
        database,
        """SELECT sql FROM sqlite_schema
           WHERE name NOT LIKE 'sqlite_%' AND sql IS NOT NULL""",
    ).fetchall()
    if {normalized(row[0]) for row in rows} != {
        normalized(statement) for statement in statements_at(version)
    }:
        code = "database_schema_mismatch"
        raise StoreError(code)


def _supported_version(database: Connection, *, adopt_legacy: bool) -> int:
    application, version = _header(database)
    if application == APPLICATION_ID:
        if version > SCHEMA_VERSION:
            code = "database_schema_too_new"
            raise StoreError(code)
        if version < 1:
            code = "database_schema_mismatch"
            raise StoreError(code)
        _check_schema(database, version)
        return version
    if application != 0 or version != 0:
        code = "foreign_database"
        raise StoreError(code)
    rows = text_cursor(database, "SELECT name FROM sqlite_schema").fetchall()
    if not rows:
        return 0
    _check_schema(database, 1)
    if not adopt_legacy:
        code = "legacy_adoption_required"
        raise StoreError(code)
    return 1


def initialize(path: Path, *, adopt_legacy: bool = False) -> None:
    """Upgrade only recognized data; unknown repositories receive no DDL or WAL change."""
    with closing(connect(path, timeout=5.0)) as database, database:
        _ = database.execute("BEGIN IMMEDIATE")
        version = _supported_version(database, adopt_legacy=adopt_legacy)
        for migration in MIGRATIONS[version:]:
            for statement in migration:
                _ = database.execute(statement)
        _check_schema(database, SCHEMA_VERSION)
        _ = database.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        _ = database.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    # Foreign and future schemas never reach this point. WAL cannot run in the transaction.
    with closing(connect(path)) as database:
        _ = database.execute("PRAGMA journal_mode = WAL")


@contextmanager
def connection(path: Path) -> Generator[Connection, None, None]:
    """Open a current repository without creating, upgrading or dispatching anything."""
    uri = f"{path.resolve().as_uri()}?mode=rw"
    with closing(connect(uri, uri=True, timeout=5.0)) as database:
        application, version = _header(database)
        if application != APPLICATION_ID or version != SCHEMA_VERSION:
            code = "database_version_not_current"
            raise StoreError(code)
        _check_schema(database, version)
        _ = database.execute("PRAGMA foreign_keys = ON")
        yield database
