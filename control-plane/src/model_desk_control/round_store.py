"""Durable planning rounds reuse the existing continuation outbox and authority."""

from dataclasses import dataclass
from typing import assert_never

from model_desk_control.authority import read_control
from model_desk_control.database import StoreError
from model_desk_control.lease_rules import ContinuationLease, lease_current
from model_desk_control.outbox import OutboxStore
from model_desk_control.round_feedback import accept_feedback
from model_desk_control.round_finalize import finalize_round
from model_desk_control.round_models import (
    BrainFeedback,
    PlanningRound,
    RoundAction,
    RoundRequest,
    RoundStage,
)
from model_desk_control.round_records import read_round, require_round, write_round
from model_desk_control.store import ControlStore
from model_desk_control.task_state import read_plan


@dataclass(frozen=True, slots=True)
class RoundStore:
    """Construction is inert; every native start must first win an atomic arm."""

    controls: ControlStore

    def authorize(self, lease: ContinuationLease, *, now: int) -> bool:
        """Use the same outbox authority as every other continuation consumer."""
        return OutboxStore(self.controls).authorize(lease, now=now)

    def read(self, goal_id: str, operation_id: str) -> PlanningRound:
        """Observe a precise round without refreshing providers or creating a record."""
        with self.controls.connection() as database:
            return require_round(database, goal_id, operation_id)

    def prepare(self, lease: ContinuationLease, *, now: int) -> PlanningRound | None:
        """Capture the original input once, before calling any native runner."""
        with self.controls.connection() as database, database:
            _ = database.execute("BEGIN IMMEDIATE")
            existing = read_round(database, lease.goal_id, lease.operation_id)
            if existing is not None:
                if existing.request.control.epoch != lease.epoch:
                    code = "planning_round_epoch_mismatch"
                    raise StoreError(code)
                return existing
            if not lease_current(database, lease, now):
                return None
            control = read_control(database, lease.goal_id)
            if not control.busy:
                code = "continuation_not_active"
                raise StoreError(code)
            record = PlanningRound(
                request=RoundRequest(
                    operation_id=lease.operation_id,
                    control=control,
                    plan=read_plan(database, lease.goal_id),
                )
            )
            _ = database.execute(
                "INSERT INTO planning_rounds VALUES (?, ?, ?)",
                (lease.operation_id, lease.goal_id, record.model_dump_json()),
            )
            return record

    def choose(self, lease: ContinuationLease, *, now: int) -> tuple[RoundAction, PlanningRound]:
        """Choose one first start; all later unresolved entries only inspect the original."""
        with self.controls.connection() as database, database:
            _ = database.execute("BEGIN IMMEDIATE")
            record = require_round(database, lease.goal_id, lease.operation_id)
            if record.request.control.epoch != lease.epoch:
                code = "planning_round_epoch_mismatch"
                raise StoreError(code)
            match record.stage:
                case RoundStage.PREPARED:
                    if not lease_current(database, lease, now):
                        return RoundAction.REVOKED, record
                    plan = read_plan(database, lease.goal_id)
                    if plan.revision != record.request.plan.revision:
                        return RoundAction.REVOKED, record
                    armed = record.model_copy(
                        update={
                            "stage": RoundStage.IN_FLIGHT,
                            "armed": True,
                            "revision": record.revision + 1,
                        }
                    )
                    write_round(database, armed)
                    return RoundAction.START, armed
                case RoundStage.IN_FLIGHT | RoundStage.UNKNOWN:
                    return RoundAction.INSPECT, record
                case (
                    RoundStage.RESULT
                    | RoundStage.FAILED
                    | RoundStage.NOT_STARTED
                    | RoundStage.FINALIZED
                ):
                    return RoundAction.NONE, record
                case _:
                    assert_never(record.stage)

    def observe(self, goal_id: str, feedback: BrainFeedback) -> PlanningRound:
        """Archive a trusted runner receipt even if its goal has since been stopped."""
        with self.controls.connection() as database, database:
            _ = database.execute("BEGIN IMMEDIATE")
            return accept_feedback(database, goal_id, feedback)

    def finalize(self, lease: ContinuationLease) -> PlanningRound:
        """Commit plan, settlement and busy release atomically, never close the goal."""
        with self.controls.connection() as database, database:
            _ = database.execute("BEGIN IMMEDIATE")
            return finalize_round(database, lease)
