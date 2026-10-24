"""Lease pending effects; expired or stopped leases require reconciliation."""

from dataclasses import dataclass

from model_desk_control.control import Control, UserIntent
from model_desk_control.lease_rules import ContinuationLease, lease_current
from model_desk_control.store import ControlStore, StoreError, text_cursor

__all__ = ["ContinuationLease", "OutboxStore"]


@dataclass(frozen=True, slots=True)
class OutboxStore:
    """Explicit consumers share the authority DB, not a competing scheduler."""

    controls: ControlStore

    def take(
        self,
        *,
        goal_id: str,
        owner: str,
        now: int,
        lease_seconds: int,
    ) -> ContinuationLease | None:
        """Lease at most one unsent intent under current user authority."""
        if not owner or lease_seconds <= 0 or now < 0:
            code = "invalid_lease_parameters"
            raise StoreError(code)
        with self.controls.connection() as database, database:
            _ = database.execute("BEGIN IMMEDIATE")
            control = ControlStore.read_from(database, goal_id)
            if control.closed or control.intent is not UserIntent.RUN or control.resource_hold:
                return None
            cursor = text_cursor(
                database,
                """SELECT operation_id, document FROM outbox
                   WHERE goal_id = ? AND epoch = ? AND state = 'pending'
                   ORDER BY operation_id LIMIT 1""",
                (goal_id, str(control.epoch)),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            snapshot = Control.model_validate_json(row[1])
            if snapshot.goal_id != goal_id or snapshot.epoch != control.epoch:
                code = "intent_authority_mismatch"
                raise StoreError(code)
            lease = ContinuationLease(
                operation_id=row[0],
                goal_id=goal_id,
                epoch=control.epoch,
                owner=owner,
                expires_at=now + lease_seconds,
            )
            _ = database.execute(
                """UPDATE outbox SET state = 'leased', owner = ?, lease_until = ?
                   WHERE operation_id = ? AND state = 'pending'""",
                (owner, lease.expires_at, lease.operation_id),
            )
            return lease

    def authorize(self, lease: ContinuationLease, *, now: int) -> bool:
        """Recheck immediately before a controlled effect; do not reuse an old receipt."""
        with self.controls.connection() as database:
            _ = database.execute("BEGIN")
            return lease_current(database, lease, now)

    def renew(self, lease: ContinuationLease, *, now: int, lease_seconds: int) -> ContinuationLease:
        """Extend only a currently owned generation; expiry never grants a new owner."""
        if lease_seconds <= 0:
            code = "invalid_lease_parameters"
            raise StoreError(code)
        with self.controls.connection() as database, database:
            _ = database.execute("BEGIN IMMEDIATE")
            if not lease_current(database, lease, now):
                code = "lease_not_current"
                raise StoreError(code)
            renewed = lease.model_copy(
                update={"expires_at": max(lease.expires_at + 1, now + lease_seconds)}
            )
            _ = database.execute(
                "UPDATE outbox SET lease_until = ? WHERE operation_id = ?",
                (renewed.expires_at, lease.operation_id),
            )
            return renewed

    def expire(self, *, now: int) -> int:
        """Record uncertainty after owner loss; never automatically resend the effect."""
        with self.controls.connection() as database, database:
            cursor = database.execute(
                """UPDATE outbox SET state = 'unknown'
                   WHERE state = 'leased' AND lease_until <= ?""",
                (now,),
            )
            return cursor.rowcount
