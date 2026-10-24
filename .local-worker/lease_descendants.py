"""Persist observed descendants so reparenting and setsid do not erase known owners.

Darwin kqueue fork notices close the silent short-parent sampling gap by
fencing same-user processes reparented to PID 1 in the notice window. These
conservative candidates are never treated as kill authorization. Other POSIX
hosts retain sampling.
"""

import os
import select
import sys
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from sqlite3 import Connection
from threading import Event, Thread
from typing import Final, Literal

import lease_storage
import psutil
from model_desk_control.database import text_cursor
from model_desk_control.durable_lease_models import (
    DurableLease,
    LeaseStoreError,
    require_process,
)
from model_desk_control.host_flow import HostRecord
from model_desk_control.host_queue_models import Process
from pydantic import model_validator

LIMIT: Final = 1024
INTERVAL: Final = 0.2


class Observation(HostRecord):
    """No prompts, environment, command lines or account data are recorded."""

    token: str
    launcher: Process
    state: Literal["observing", "closed", "unknown"] = "observing"
    descendants: tuple[Process, ...] = ()
    fork_guard: Literal["sampled", "darwin_kqueue"] = "sampled"
    unattributed_fork_parents: tuple[int, ...] = ()
    suspected_orphans: tuple[Process, ...] = ()
    uncertainty_reason: str = ""

    @model_validator(mode="after")
    def identities(self) -> "Observation":
        """Require reusable-PID-safe identities and a bounded observation set."""
        require_process(self.launcher)
        for process in (*self.descendants, *self.suspected_orphans):
            require_process(process)
        if len(self.descendants) + len(self.suspected_orphans) > LIMIT:
            code = "lease_descendant_limit"
            raise LeaseStoreError(code)
        return self


def initialize(database: Connection, token: str, launcher: Process) -> None:
    """Commit observation identity together with the worker's started receipt."""
    _ = database.execute(
        "CREATE TABLE IF NOT EXISTS lease_descendants(token TEXT PRIMARY KEY, data TEXT NOT NULL)"
    )
    value = Observation(token=token, launcher=launcher)
    _ = database.execute(
        "INSERT INTO lease_descendants VALUES(?, ?)", (token, value.model_dump_json())
    )


def read(database: Connection, receipt: DurableLease) -> Observation | None:
    """Missing historical coverage remains unknown, never silently upgraded."""
    table = text_cursor(
        database, "SELECT name FROM sqlite_master WHERE type='table' AND name='lease_descendants'"
    ).fetchone()
    if table is None:
        return None
    row = text_cursor(
        database, "SELECT data FROM lease_descendants WHERE token=?", (receipt.token,)
    ).fetchone()
    if row is None:
        return None
    value = Observation.model_validate_json(row[0])
    if value.token != receipt.token or value.launcher != receipt.launcher:
        code = "lease_descendant_identity_changed"
        raise LeaseStoreError(code)
    return value


def _live(identity: Process) -> psutil.Process | None:
    try:
        process = psutil.Process(identity.pid)
        if process.create_time() != identity.birth or process.status() == psutil.STATUS_ZOMBIE:
            return None
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return None
    return process


def _scan(previous: Observation) -> tuple[Process, ...]:
    seen = {(process.pid, process.birth): process for process in previous.descendants}
    scanned: set[tuple[int, float | None]] = set()
    for identity in (previous.launcher, *previous.descendants):
        if (identity.pid, identity.birth) in scanned:
            continue
        parent = _live(identity)
        if parent is None:
            continue
        try:
            children = parent.children(recursive=True)
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        for child in children:
            try:
                process = Process(pid=child.pid, birth=child.create_time())
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
            seen[(process.pid, process.birth)] = process
            scanned.add((process.pid, process.birth))
            if len(seen) > LIMIT:
                code = "lease_descendant_limit"
                raise LeaseStoreError(code)
    return tuple(seen.values())


def _unattributed(
    previous: Observation, observed: tuple[Process, ...], fork_parents: set[int]
) -> tuple[int, ...]:
    """Return fork notices that cannot be bound to a newly visible direct child."""
    known = {(process.pid, process.birth) for process in previous.descendants}
    children: set[int] = set()
    for identity in observed:
        if (identity.pid, identity.birth) in known:
            continue
        live = _live(identity)
        if live is None:
            continue
        try:
            children.add(live.ppid())
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
    return tuple(sorted(fork_parents - children))


def _recent_orphans(previous: Observation, since: float) -> tuple[Process, ...]:
    """Find live same-user processes reparented during a kernel-notified fork window.

    These are conservative candidates only. They fence release but are never
    signalled as owned descendants because macOS does not expose the child PID
    in NOTE_FORK.
    """
    excluded = {
        (process.pid, process.birth)
        for process in (
            previous.launcher,
            *previous.descendants,
            *previous.suspected_orphans,
        )
    }
    candidates: list[Process] = []
    for process in psutil.process_iter(["pid", "ppid", "create_time", "uids"]):
        try:
            info = process.info
            uids = info.get("uids")
            birth = float(info.get("create_time") or 0)
            if (
                int(info.get("ppid") or -1) != 1
                or birth < since
                or uids is None
                or uids.real != os.getuid()
            ):
                continue
            identity = Process(pid=int(info["pid"]), birth=birth)
            if (identity.pid, identity.birth) not in excluded:
                candidates.append(identity)
        except (KeyError, TypeError, ValueError, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
    return tuple(candidates)


def sample(
    home: Path,
    receipt: DurableLease,
    *,
    final: bool = False,
    fork_parents: set[int] | None = None,
    fork_since: float | None = None,
) -> Observation:
    """Record before allowing final worker exit; failures leave coverage uncertain."""
    with lease_storage.transaction(home) as database:
        previous = read(database, receipt)
        if previous is None:
            code = "lease_descendant_receipt_missing"
            raise LeaseStoreError(code)
        if previous.state != "observing":
            return previous
        try:
            observed = _scan(previous)
            missing = _unattributed(previous, observed, fork_parents or set())
            candidates = previous.suspected_orphans
            if fork_parents and fork_since is not None:
                found = _recent_orphans(previous, fork_since)
                by_identity = {
                    (process.pid, process.birth): process
                    for process in (*candidates, *found)
                }
                candidates = tuple(by_identity.values())
            value = Observation(
                token=previous.token, launcher=previous.launcher, descendants=observed,
                state="closed" if final else "observing",
                fork_guard=previous.fork_guard,
                unattributed_fork_parents=tuple(sorted({
                    *previous.unattributed_fork_parents,
                    *missing,
                })),
                suspected_orphans=candidates,
                uncertainty_reason="suspected_orphan" if candidates else "",
            )
        except (psutil.Error, OSError, ValueError) as error:
            value = Observation(
                token=previous.token, launcher=previous.launcher,
                descendants=previous.descendants, state="unknown",
                fork_guard=previous.fork_guard,
                suspected_orphans=previous.suspected_orphans,
                uncertainty_reason="process_inspection:" + type(error).__name__,
            )
        if value == previous:
            return value
        _ = database.execute(
            "UPDATE lease_descendants SET data=? WHERE token=? AND data=?",
            (value.model_dump_json(), receipt.token, previous.model_dump_json()),
        )
        return value


class _ForkMonitor:
    """Darwin kernel notice source; it never guesses a child PID."""

    def __init__(self, launcher: Process) -> None:
        if sys.platform != "darwin" or not hasattr(select, "kqueue"):
            raise NotImplementedError
        self.queue = select.kqueue()
        self.registered: set[int] = set()
        self.register(launcher)

    def register(self, identity: Process) -> None:
        if identity.pid in self.registered or _live(identity) is None:
            return
        event = select.kevent(
            identity.pid,
            filter=select.KQ_FILTER_PROC,
            flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_CLEAR,
            fflags=select.KQ_NOTE_FORK,
        )
        _ = self.queue.control([event], 0, 0)
        self.registered.add(identity.pid)

    def wait(self, timeout: float) -> set[int]:
        return {
            int(event.ident)
            for event in self.queue.control(None, LIMIT, timeout)
            if event.fflags & select.KQ_NOTE_FORK
        }

    def close(self) -> None:
        self.queue.close()


def _activate_guard(home: Path, receipt: DurableLease) -> _ForkMonitor | None:
    if sys.platform != "darwin":
        return None
    monitor = _ForkMonitor(receipt.launcher)
    with lease_storage.transaction(home) as database:
        previous = read(database, receipt)
        if previous is None:
            code = "lease_descendant_receipt_missing"
            raise LeaseStoreError(code)
        value = previous.model_copy(update={"fork_guard": "darwin_kqueue"})
        _ = database.execute(
            "UPDATE lease_descendants SET data=? WHERE token=? AND data=?",
            (value.model_dump_json(), receipt.token, previous.model_dump_json()),
        )
    return monitor


def drained(receipt: DurableLease, exited: Callable[[Process], bool]) -> bool:
    """Known live descendants or incomplete observation veto automatic release."""
    try:
        with lease_storage.readonly() as database:
            value = read(database, receipt) if database is not None else None
        return (
            value is not None and value.state == "closed"
            and all(
                exited(process)
                for process in (*value.descendants, *value.suspected_orphans)
            )
        )
    except (OSError, ValueError):
        return False


@contextmanager
def observe(home: Path, receipt: DurableLease) -> Generator[None]:
    """Observe only this worker's tree; start no service or continuation timer."""
    stopped = Event()
    failed = Event()
    monitor = _activate_guard(home, receipt)

    def watch() -> None:
        while not stopped.is_set():
            try:
                fork_parents = monitor.wait(INTERVAL) if monitor is not None else set()
                value = sample(
                    home,
                    receipt,
                    fork_parents=fork_parents,
                    fork_since=time.time() - INTERVAL - 0.1 if fork_parents else None,
                )
                if value.state == "unknown":
                    failed.set()
                    stopped.set()
                    break
                if monitor is not None:
                    for identity in value.descendants:
                        monitor.register(identity)
                if stopped.is_set():
                    break
            except (OSError, ValueError):
                # No final close is written on this path; recovery remains fenced.
                failed.set()
                stopped.set()

    watcher = Thread(target=watch, name="lease-descendants", daemon=True)
    watcher.start()
    try:
        yield
    finally:
        stopped.set()
        watcher.join()
        try:
            if not failed.is_set():
                fork_parents = monitor.wait(0) if monitor is not None else set()
                sample(
                    home,
                    receipt,
                    final=True,
                    fork_parents=fork_parents,
                    fork_since=time.time() - INTERVAL - 0.1 if fork_parents else None,
                )
        finally:
            if monitor is not None:
                monitor.close()
