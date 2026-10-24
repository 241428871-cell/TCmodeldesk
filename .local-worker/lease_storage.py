"""One installation-owned resource store, opened only by explicit lease operations."""

import os
from collections.abc import Generator
from contextlib import closing, contextmanager
from pathlib import Path
from sqlite3 import Connection, Error, connect
from typing import Final
from uuid import uuid4

import model_registry as registry
from model_desk_control.database import text_cursor
from model_desk_control.durable_lease_models import LeaseStoreError
from model_desk_control.host_initialization_files import sync_directory

APPLICATION: Final = 0x4141444C
IDENTITY_SIZE: Final = 32
INITIALIZING: Final = "initializing"
READY: Final = "ready"


def path(home: Path | None = None) -> Path:
    """Use an installation locator, never task content, to select the shared store."""
    root = registry.HERE if home is None else home
    if root.is_symlink() or not root.is_dir():
        code = "lease_installation_invalid"
        raise LeaseStoreError(code)
    target = root.resolve() / "resource-leases.db"
    for suffix in (
        "",
        "-wal",
        "-shm",
        "-journal",
        ".identity",
        ".identity.tmp",
        ".initializing",
        ".initializing-wal",
        ".initializing-shm",
        ".initializing-journal",
        ".lock",
    ):
        candidate = target.with_name(target.name + suffix)
        if candidate.is_symlink() or (candidate.exists() and not candidate.is_file()):
            code = "lease_store_path_invalid"
            raise LeaseStoreError(code)
    return target


def _marker(target: Path) -> tuple[str, str]:
    with target.with_suffix(".db.identity").open(encoding="ascii") as handle:
        value = handle.read(80)
    if len(value) == IDENTITY_SIZE:
        state, identity = READY, value
    else:
        parts = value.split(":", maxsplit=1)
        if len(parts) != 2:
            code = "lease_store_identity_invalid"
            raise LeaseStoreError(code)
        state, identity = parts
    if (
        state not in (INITIALIZING, READY)
        or len(identity) != IDENTITY_SIZE
        or any(char not in "0123456789abcdef" for char in identity)
    ):
        code = "lease_store_identity_invalid"
        raise LeaseStoreError(code)
    return state, identity


def _publish_marker(marker: Path, state: str, identity: str) -> None:
    temporary = marker.with_name(marker.name + ".tmp")
    try:
        temporary.unlink(missing_ok=True)
        with temporary.open("x", encoding="ascii") as handle:
            _ = handle.write(f"{state}:{identity}")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, marker)
        sync_directory(marker.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _validate(database: Connection, identity: str) -> None:
    header = text_cursor(
        database, "SELECT CAST(application_id AS TEXT) FROM pragma_application_id"
    ).fetchone()
    if header is None or int(header[0]) != APPLICATION:
        code = "lease_foreign_store"
        raise LeaseStoreError(code)


def _initialize_database(target: Path, identity: str) -> None:
    """Publish a complete database while the durable marker still fences all use."""
    temporary = target.with_name(target.name + ".initializing")
    for suffix in ("", "-wal", "-shm", "-journal"):
        temporary.with_name(temporary.name + suffix).unlink(missing_ok=True)
    try:
        with closing(connect(temporary, timeout=5)) as database, database:
            _ = database.execute("PRAGMA synchronous=FULL")
            _ = database.execute("BEGIN IMMEDIATE")
            _ = database.execute("CREATE TABLE lease_store_identity(identity TEXT NOT NULL)")
            _ = database.execute("INSERT INTO lease_store_identity VALUES(?)", (identity,))
            _ = database.execute(f"PRAGMA application_id={APPLICATION}")
            _validate(database, identity)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        sync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _ensure_ready(target: Path) -> str:
    marker = target.with_suffix(".db.identity")
    if not marker.exists() and not target.exists():
        identity = uuid4().hex
        _publish_marker(marker, INITIALIZING, identity)
        state = INITIALIZING
    elif not marker.exists():
        code = "lease_store_missing_or_unrecognized"
        raise LeaseStoreError(code)
    else:
        state, identity = _marker(target)

    if state == READY:
        if not target.exists():
            code = "lease_store_missing_or_unrecognized"
            raise LeaseStoreError(code)
        return identity

    if not target.exists():
        _initialize_database(target, identity)
    else:
        try:
            with closing(connect(target.as_uri() + "?mode=ro", uri=True, timeout=5)) as database:
                _validate(database, identity)
        except Error as error:
            code = "lease_store_initialization_incomplete"
            raise LeaseStoreError(code) from error
    sync_directory(target.parent)
    _publish_marker(marker, READY, identity)
    return identity
    saved = text_cursor(database, "SELECT identity FROM lease_store_identity").fetchall()
    if len(saved) != 1 or saved[0][0] != identity:
        code = "lease_store_identity_changed"
        raise LeaseStoreError(code)


@contextmanager
def transaction(home: Path | None = None) -> Generator[Connection]:
    """Serialize short transitions and refuse a lost/replaced database after initialization."""
    target = path(home)
    with registry.file_lock(target.with_suffix(".db.lock")):
        identity = _ensure_ready(target)
        try:
            with closing(connect(target, timeout=5)) as database, database:
                _ = database.execute("PRAGMA synchronous=FULL")
                _ = database.execute("BEGIN IMMEDIATE")
                _validate(database, identity)
                yield database
        except Error as error:
            code = "lease_store_unavailable"
            raise LeaseStoreError(code) from error


@contextmanager
def readonly(home: Path | None = None) -> Generator[Connection | None]:
    """Read status without creating files, locks or schema."""
    target = path(home)
    marker = target.with_suffix(".db.identity")
    if not target.exists() and not marker.exists():
        yield None
        return
    if not target.exists() or not marker.exists():
        code = "lease_store_missing_or_unrecognized"
        raise LeaseStoreError(code)
    state, identity = _marker(target)
    if state != READY:
        code = "lease_store_initializing"
        raise LeaseStoreError(code)
    try:
        with closing(connect(target.as_uri() + "?mode=ro", uri=True, timeout=5)) as database:
            _validate(database, identity)
            yield database
    except Error as error:
        code = "lease_store_unavailable"
        raise LeaseStoreError(code) from error
