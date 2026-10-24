"""Persistence and fencing for the single continuation owner of a project goal."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import TypedDict
from uuid import uuid4

import model_registry as registry
import project_storage
import stage_store

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_LOCK = threading.Lock()
_HELD = threading.local()


class IntentRecord(TypedDict, total=False):
    id: str
    intent_id: str
    goal_id: str
    authority_epoch: int
    owner_epoch: int
    owner_id: str
    status: str
    created_at: str
    lease_until: str
    pid: int | None
    birth: float | None
    brain_model_id: str
    native_session_id: str
    brain_permission_mode: str
    host_protocol: str
    trigger_kind: str
    event_ids: list[str]
    requirements_revision: int
    finished_at: str
    result: dict
    reconciled_at: str
    evidence: str


class ClockSnapshot(TypedDict, total=False):
    schema: int
    exists: bool
    project: str
    goal_id: str
    clock_id: str
    enabled: bool
    user_intent: str
    authority_epoch: int
    owner_epoch: int
    interval_seconds: int
    next_due_at: str | None
    event_not_before: str | None
    hold: dict
    closed: bool
    terminal_kind: str
    active_intent_id: str
    active_intent: IntentRecord | None
    brain_model_id: str
    native_session_id: str
    brain_permission_mode: str
    obligations: list
    requirements_revision: int
    created_at: str
    updated_at: str
    last_result: dict
    last_round_at: str
    status: str


class ClockError(ValueError):
    """Continuation state rejected an unsafe or stale operation."""


class RequirementsChangedError(ClockError):
    """The active brain result was produced from an older requirement revision."""


@contextmanager
def readonly(project: str):
    """Read the existing database without migrations, WAL changes or lock writes."""
    path = stage_store.db_path(project)
    if not path.is_file():
        yield None
        return
    conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=15,
                           isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def timestamp(value: str | None) -> float:
    """Parse an ISO UTC receipt; absent dates are not due."""
    return datetime.fromisoformat(value).timestamp() if value else float('inf')


def iso(value: float | None = None) -> str:
    """Format a UTC timestamp without a machine-specific timezone."""
    return datetime.fromtimestamp(time.time() if value is None else value, timezone.utc).isoformat()


def interval(value: int) -> int:
    """Validate an explicit cadence separately from timeouts or business deadlines."""
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 31536000:
        raise ClockError('续跑周期须为 1 至 31536000 秒的整数')
    return value


@contextmanager
def locked(project: str, goal_id: str):
    """Serialize STOP and each authority-bound effect across threads/processes."""
    project = project_storage.normalize(project)
    identity = hashlib.sha256((project + '\0' + goal_id).encode()).hexdigest()
    with _LOCKS_LOCK:
        lock = _LOCKS.setdefault(identity, threading.RLock())
    with lock:
        held = getattr(_HELD, 'identities', set())
        if identity in held:
            yield
            return
        with registry.file_lock(Path(registry.HERE) / ('continuation-' + identity + '.lock')):
            _HELD.identities = held | {identity}
            try:
                yield
            finally:
                _HELD.identities = held


def schema(conn) -> None:
    """Create continuation records only inside an explicit mutation."""
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS continuation_clocks (
            goal_id TEXT PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS continuation_intents (
            id TEXT PRIMARY KEY, goal_id TEXT NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL);
        CREATE UNIQUE INDEX IF NOT EXISTS continuation_one_owner ON continuation_intents(goal_id)
            WHERE status IN ('claimed','started','unknown');
    ''')


def exists(conn) -> bool:
    """Read schema presence without initializing an untouched project."""
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='continuation_clocks'").fetchone() is not None


def goal(conn, project: str, goal_id: str) -> None:
    """Validate exact project ownership before any continuation mutation."""
    row = conn.execute('SELECT project FROM goals WHERE id=?', (goal_id,)).fetchone()
    if row is None or project_storage.normalize(row['project']) != project_storage.normalize(project):
        raise ClockError('目标不存在或不属于此项目')


def read(conn, goal_id: str) -> dict | None:  # noqa: DICT_OK - existing JSON records boundary.
    """Read one clock's current authority, not an old task snapshot."""
    if not exists(conn):
        return None
    row = conn.execute('SELECT data FROM continuation_clocks WHERE goal_id=?', (goal_id,)).fetchone()
    return json.loads(row['data']) if row else None


def intent(conn, intent_id: str) -> dict | None:  # noqa: DICT_OK - persistent JSON receipt.
    """Read a durable operation identity."""
    if not intent_id:
        return None
    row = conn.execute('SELECT data FROM continuation_intents WHERE id=?', (intent_id,)).fetchone()
    return json.loads(row['data']) if row else None


def write(conn, clock: dict) -> None:
    """Persist the single clock record inside the caller's transaction."""
    clock['updated_at'] = iso()
    conn.execute('INSERT INTO continuation_clocks(goal_id,data) VALUES (?,?) '
                 'ON CONFLICT(goal_id) DO UPDATE SET data=excluded.data',
                 (clock['goal_id'], json.dumps(clock, ensure_ascii=False)))


def write_intent(conn, record: dict) -> None:
    """Persist an operation receipt; its primary identity never changes."""
    conn.execute('INSERT INTO continuation_intents(id,goal_id,status,data) VALUES (?,?,?,?) '
                 'ON CONFLICT(id) DO UPDATE SET status=excluded.status,data=excluded.data',
                 (record['id'], record['goal_id'], record['status'], json.dumps(record, ensure_ascii=False)))


def new_clock(project: str, goal_id: str, seconds: int) -> dict:  # noqa: DICT_OK - durable record.
    """Build a disabled clock without dispatching or inventing a deadline."""
    return {'schema': 1, 'exists': True, 'project': project, 'goal_id': goal_id,
            'clock_id': 'clock_' + uuid4().hex, 'enabled': False, 'user_intent': 'USER_PAUSE',
            'authority_epoch': 1, 'owner_epoch': 0, 'owner_history': [], 'interval_seconds': seconds, 'next_due_at': None,
            'hold': {}, 'closed': False, 'terminal_kind': '', 'active_intent_id': '',
            'brain_model_id': '', 'native_session_id': '', 'brain_permission_mode': 'auto', 'obligations': [],
            'requirements_revision': 0,
            'created_at': iso(), 'updated_at': iso(), 'last_result': {}}


@contextmanager
def mutation(project: str, goal_id: str):
    """Open the exact existing project and make one bounded atomic state change."""
    project = project_storage.normalize(project)
    with locked(project, goal_id), stage_store.connection(project, create=False) as conn:
        goal(conn, project, goal_id)
        schema(conn)
        with stage_store._transaction(conn):
            yield conn


def active(conn, clock: dict) -> dict | None:  # noqa: DICT_OK - receipt boundary.
    """Return the retained owner, including a revoked owner awaiting reconciliation."""
    return intent(conn, clock.get('active_intent_id', ''))


def require_epoch(clock: dict, record: dict | None, epoch: int) -> None:
    """Reject stale or foreign receipts before touching any current state."""
    if (record is None or record['goal_id'] != clock['goal_id']
            or record['authority_epoch'] != epoch or clock['authority_epoch'] != epoch
            or record.get('owner_epoch', 0) != clock.get('owner_epoch', 0)
            or record['id'] != clock['active_intent_id']):
        raise ClockError('续跑授权已变化，此旧轮次无权更新当前任务')
