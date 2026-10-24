"""SQLite authority and atomic continuation intent; no native process dispatch."""

from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from sqlite3 import Connection

from model_desk_control.authority import read_control, write_control
from model_desk_control.control import Control, UserIntent, claim_tick, stop
from model_desk_control.database import StoreError, connection, initialize, text_cursor
from model_desk_control.decision_rules import revoke_goal_decisions
from model_desk_control.plans import Plan
from model_desk_control.receipts import fingerprint, record, replay

__all__ = ["ControlStore", "StoreError", "text_cursor"]


@dataclass(frozen=True, slots=True)
class ControlStore:
    """Explicit local repository; constructing it neither opens nor creates a DB."""

    path: Path

    def initialize(self, *, adopt_legacy: bool = False) -> None:
        """Create only an explicitly requested repository, never on read or import."""
        initialize(self.path, adopt_legacy=adopt_legacy)

    @contextmanager
    def connection(self) -> Generator[Connection, None, None]:
        """Open an existing local database and close it after every command."""
        with connection(self.path) as database:
            yield database

    def create(self, state: Control) -> None:
        """Create an exact goal identity; callers own request creation authority."""
        with self.connection() as database, database:
            _ = database.execute(
                "INSERT INTO controls VALUES (?, ?, ?, ?)",
                (state.goal_id, state.epoch, state.revision, state.model_dump_json()),
            )
            _ = database.execute(
                "INSERT INTO plans VALUES (?, 0, ?)", (state.goal_id, Plan().model_dump_json())
            )

    def read(self, goal_id: str) -> Control:
        """Load current authority without creating records or contacting providers."""
        with self.connection() as database:
            return self.read_from(database, goal_id)

    @staticmethod
    def read_from(database: Connection, goal_id: str) -> Control:
        """Read authority inside an existing transaction without opening another one."""
        return read_control(database, goal_id)

    def _change(
        self,
        *,
        goal_id: str,
        request_id: str,
        payload: str,
        transform: Callable[[Control], Control],
        emit_continuation: bool,
    ) -> Control:
        payload_hash = fingerprint(goal_id, payload)
        with self.connection() as database, database:
            _ = database.execute("BEGIN IMMEDIATE")
            response = replay(database, request_id, payload_hash)
            if response is not None:
                return Control.model_validate_json(response)
            prior = self.read_from(database, goal_id)
            changed = transform(prior)
            write_control(database, prior, changed)
            if emit_continuation:
                operation_id = f"{goal_id}:continuation:{changed.epoch}:{changed.revision}"
                _ = database.execute(
                    """INSERT INTO outbox (operation_id, goal_id, epoch, document, state)
                       VALUES (?, ?, ?, ?, 'pending')""",
                    (operation_id, goal_id, changed.epoch, changed.model_dump_json()),
                )
            if changed.intent is UserIntent.STOP:
                _ = database.execute(
                    """UPDATE outbox SET state = CASE state
                       WHEN 'pending' THEN 'cancelled' ELSE 'unknown' END
                       WHERE goal_id = ? AND state IN ('pending', 'leased')""",
                    (goal_id,),
                )
                _ = revoke_goal_decisions(database, goal_id)
            record(
                database,
                request_id=request_id,
                goal_id=goal_id,
                payload_hash=payload_hash,
                response=changed.model_dump_json(),
            )
            return changed

    def claim(
        self, *, goal_id: str, request_id: str, epoch: int, revision: int, now: int
    ) -> Control:
        """Atomically claim one current due tick, its receipt and one outbox intent."""
        return self._change(
            goal_id=goal_id,
            request_id=request_id,
            payload=f"tick:{epoch}:{revision}:{now}",
            transform=lambda state: claim_tick(state, epoch=epoch, revision=revision, now=now),
            emit_continuation=True,
        )

    def stop(self, *, goal_id: str, request_id: str) -> Control:
        """Persist one latest explicit stop; this does not terminate an external process."""
        return self._change(
            goal_id=goal_id,
            request_id=request_id,
            payload="stop",
            transform=stop,
            emit_continuation=False,
        )

    def pending_count(self, goal_id: str) -> int:
        """Observe pending intents; callers still need an epoch-fenced consumer."""
        with self.connection() as database:
            cursor = text_cursor(
                database,
                "SELECT CAST(count(*) AS TEXT) FROM outbox WHERE goal_id = ? AND state = 'pending'",
                (goal_id,),
            )
            row = cursor.fetchone()
            if row is None:
                code = "count_result_missing"
                raise StoreError(code)
            return int(row[0])
