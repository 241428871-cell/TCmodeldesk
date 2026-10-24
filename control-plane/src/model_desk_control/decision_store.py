"""One scoped exception card, one human answer and one idempotent task consumption."""

from collections.abc import Callable
from dataclasses import dataclass
from sqlite3 import Connection

from model_desk_control.commands import GoalCommand, require_running
from model_desk_control.database import StoreError, text_cursor
from model_desk_control.decision_rules import (
    invalid_reason,
    invalidate_stale,
    open_decisions,
    read_decision,
    write_decision,
)
from model_desk_control.decisions import (
    AnswerDecision,
    ConsumeDecision,
    Decision,
    DecisionScope,
    DecisionStatus,
    RaiseDecision,
)
from model_desk_control.receipts import fingerprint, record, replay
from model_desk_control.store import ControlStore
from model_desk_control.task_state import (
    TaskStatus,
    live_task,
    read_plan,
    read_task,
    task_digest,
    write_task,
)


def _current(database: Connection, command: AnswerDecision | ConsumeDecision) -> Decision:
    decision = read_decision(database, command.goal_id, command.decision_id)
    if decision.revision != command.decision_revision:
        code = "decision_revision_conflict"
        raise StoreError(code)
    reason = invalid_reason(database, decision.scope)
    if reason is not None:
        raise StoreError(reason)
    return decision


@dataclass(frozen=True, slots=True)
class DecisionStore:
    """The caller adapter separates human answer authority from the brain tool surface."""

    controls: ControlStore

    def _change(
        self, command: GoalCommand, kind: str, transform: Callable[[Connection], Decision]
    ) -> Decision:
        payload_hash = fingerprint(command.goal_id, f"{kind}:{command.model_dump_json()}")
        with self.controls.connection() as database, database:
            _ = database.execute("BEGIN IMMEDIATE")
            response = replay(database, command.request_id, payload_hash)
            if response is not None:
                return Decision.model_validate_json(response)
            _ = require_running(database, command)
            changed = transform(database)
            record(
                database,
                request_id=command.request_id,
                goal_id=command.goal_id,
                payload_hash=payload_hash,
                response=changed.model_dump_json(),
            )
            return changed

    def raise_exception(self, command: RaiseDecision) -> Decision:
        """Deduplicate the same relevant question; unrelated tasks keep running."""

        def create(database: Connection) -> Decision:
            plan = read_plan(database, command.goal_id)
            task = live_task(plan, command.proposal.task_id)
            state = read_task(database, command.goal_id, task.task_id)
            if state.status in (TaskStatus.COMPLETED, TaskStatus.CANCELLED):
                code = "task_finished"
                raise StoreError(code)
            scope = DecisionScope(
                goal_id=command.goal_id,
                task_id=task.task_id,
                epoch=command.epoch,
                plan_revision=plan.revision,
                task_hash=task_digest(task),
                context_revision=state.context_revision,
                policy_revision=state.policy_revision,
                artifact_hash=state.artifact_hash,
            )
            subject_hash = fingerprint(
                command.goal_id,
                scope.model_dump_json(exclude={"plan_revision"})
                + command.proposal.model_dump_json(),
            )
            row = text_cursor(
                database,
                "SELECT decision_id FROM decisions WHERE goal_id = ? AND subject_hash = ?",
                (command.goal_id, subject_hash),
            ).fetchone()
            if row is not None:
                return read_decision(database, command.goal_id, row[0])
            decision = Decision(
                decision_id=f"decision:{subject_hash}",
                subject_hash=subject_hash,
                scope=scope,
                proposal=command.proposal,
            )
            _ = database.execute(
                "INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?)",
                (
                    decision.decision_id,
                    command.goal_id,
                    subject_hash,
                    decision.revision,
                    decision.status,
                    decision.model_dump_json(),
                ),
            )
            if state.status is TaskStatus.PLANNED:
                write_task(
                    database,
                    command.goal_id,
                    state.model_copy(
                        update={"status": TaskStatus.WAITING, "revision": state.revision + 1}
                    ),
                )
            return decision

        return self._change(command, "raise_decision", create)

    def answer(self, command: AnswerDecision) -> Decision:
        """Persist one scoped human answer; this never resumes a graph or starts a CLI."""

        def apply(database: Connection) -> Decision:
            decision = _current(database, command)
            if decision.status is not DecisionStatus.PENDING:
                code = "decision_not_pending"
                raise StoreError(code)
            choices = {option.option_id for option in decision.proposal.options}
            if (
                command.answer.selected_option is not None
                and command.answer.selected_option not in choices
            ):
                code = "answer_option_not_available"
                raise StoreError(code)
            changed = decision.model_copy(
                update={
                    "answer": command.answer,
                    "status": DecisionStatus.ANSWERED,
                    "revision": decision.revision + 1,
                }
            )
            write_decision(database, changed)
            return changed

        return self._change(command, "answer_decision", apply)

    def consume(self, command: ConsumeDecision) -> Decision:
        """Consume once for the exact task; effects still require fresh runner authorization."""

        def apply(database: Connection) -> Decision:
            decision = _current(database, command)
            if decision.scope.task_id != command.task_id:
                code = "decision_task_mismatch"
                raise StoreError(code)
            if decision.status is not DecisionStatus.ANSWERED or decision.answer is None:
                code = "decision_not_answered"
                raise StoreError(code)
            changed = decision.model_copy(
                update={"status": DecisionStatus.CONSUMED, "revision": decision.revision + 1}
            )
            write_decision(database, changed)
            remaining = any(
                item.scope.task_id == command.task_id
                and invalid_reason(database, item.scope) is None
                for item in open_decisions(database, command.goal_id)
            )
            state = read_task(database, command.goal_id, command.task_id)
            if not remaining and state.status is TaskStatus.WAITING:
                write_task(
                    database,
                    command.goal_id,
                    state.model_copy(
                        update={"status": TaskStatus.PLANNED, "revision": state.revision + 1}
                    ),
                )
            return changed

        return self._change(command, "consume_decision", apply)

    def read(self, goal_id: str, decision_id: str) -> Decision:
        """Read current persisted status; this snapshot alone grants no authority."""
        with self.controls.connection() as database:
            return read_decision(database, goal_id, decision_id)

    def pending(self, goal_id: str) -> tuple[Decision, ...]:
        """Show only current actionable cards without mutating records on a GET."""
        with self.controls.connection() as database:
            _ = database.execute("BEGIN")
            return tuple(
                item
                for item in open_decisions(database, goal_id)
                if invalid_reason(database, item.scope) is None
            )

    def reconcile(self, goal_id: str) -> int:
        """Persist invalidation after trusted context changes, without dispatch or a clock."""
        with self.controls.connection() as database, database:
            _ = database.execute("BEGIN IMMEDIATE")
            return invalidate_stale(database, goal_id)
