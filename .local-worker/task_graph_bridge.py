"""Production stage branches on the original project store, receipts and authority."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

import continuation_clock
import continuation_store
import control_bootstrap
import human_decisions
import model_registry as registry
import stage_store
from plan_validation import is_superseded
from pydantic import JsonValue

if TYPE_CHECKING:
    from model_desk_control.host_branch import (
        BranchGate,
        BranchObservation,
        BranchOutcome,
        BranchRef,
    )
    from model_desk_control.host_flow import HostRef

type Record = dict[str, JsonValue]
type Launch = Callable[[Record, str], str]

CHECKPOINTS: Final = Path(".model-desk") / "langgraph" / "task-checkpoints.sqlite"


def checkpoint_path(project: str) -> Path:
    """Task graphs use their own file; the goal graph keeps its checkpoint connection."""
    return Path(project) / CHECKPOINTS


def _stage_problem(reference: BranchRef) -> str:
    stage = stage_store.get_stage(reference.stage_id, reference.project)
    if stage is None or stage.get("goal_id") != reference.goal_id:
        return "阶段不存在或不属于当前目标"
    return "阶段已被计划修订替代" if is_superseded(stage) else ""


@dataclass(frozen=True, slots=True)
class StageBranchHost:
    """One branch invocation inside a claimed round; effects bind to that round and key."""

    launch: Launch
    key: str

    def observe(self, reference: BranchRef) -> BranchObservation:
        """Read live authority, plan and exceptions; this method never writes."""
        from model_desk_control.host_branch import BranchGate, BranchObservation
        from model_desk_control.host_branch_store import LIVE, read

        guard = continuation_clock.dispatch_guard(
            reference.project, reference.goal_id, reference.stage_id, reference.epoch
        )
        if not guard["allowed"]:
            return BranchObservation(gate=BranchGate.REVOKED, reason=str(guard["reason"]))
        with continuation_store.readonly(reference.project) as database:
            saved = None if database is None else read(database, reference.branch_id)
        if saved is not None and saved.status not in LIVE:
            return BranchObservation(gate=BranchGate.SETTLED)
        missing = _stage_problem(reference)
        if missing:
            return BranchObservation(gate=BranchGate.BLOCKED, reason=missing)
        blockers = human_decisions.pending_for_stage(reference.project, reference.stage_id)
        pending = sorted(
            (str(row["created_at"]), str(row["id"]))
            for row in blockers
            if row.get("status") == "pending"
        )
        if pending:
            return BranchObservation(gate=BranchGate.WAITING, decision_id=pending[0][1])
        if blockers:
            return BranchObservation(
                gate=BranchGate.BLOCKED, reason="人类未批准此例外，需主脑重新规划受影响阶段"
            )
        return BranchObservation(gate=BranchGate.READY)

    def park(self, reference: BranchRef, decision_id: str) -> None:
        """Only this branch records a wait; sibling branches keep their own gates."""
        from model_desk_control.host_branch_store import BranchMark, BranchStatus, mark

        with continuation_store.mutation(reference.project, reference.goal_id) as database:
            _ = mark(
                database,
                reference.branch_id,
                BranchMark(status=BranchStatus.WAITING, decision_id=decision_id),
            )

    def dispatch(self, reference: BranchRef) -> str:
        """Use the original dispatch, approval use, initialization and queue receipts."""
        from model_desk_control.host_branch_store import BranchMark, BranchStatus, mark

        stage = stage_store.get_stage(reference.stage_id, reference.project)
        if stage is None:
            return ""
        task_id = self.launch(stage, self.key)
        outcome = (
            BranchMark(status=BranchStatus.DISPATCHED, task_id=task_id)
            if task_id
            else BranchMark(status=BranchStatus.RETIRED, reason="阶段当前不满足派发条件")
        )
        with continuation_store.mutation(reference.project, reference.goal_id) as database:
            _ = mark(database, reference.branch_id, outcome)
        return task_id

    def retire(self, reference: BranchRef, gate: BranchGate, reason: str) -> None:
        """A revoked or blocked branch closes; a later round must open a new one."""
        from model_desk_control.host_branch_store import BranchMark, BranchStatus, mark

        with continuation_store.mutation(reference.project, reference.goal_id) as database:
            _ = mark(
                database,
                reference.branch_id,
                BranchMark(status=BranchStatus.RETIRED, reason=(gate.value + ": " + reason)[:1000]),
            )


def _advance(
    scope: HostRef, branch: BranchRef, host: StageBranchHost, awaited: str
) -> BranchOutcome:
    """Lock order: goal authority, then this branch, then its checkpoint file."""
    from langgraph.checkpoint.sqlite import SqliteSaver

    from model_desk_control.host_branch import advance_branch, build_branch_flow

    path = checkpoint_path(scope.project)
    path.parent.mkdir(parents=True, exist_ok=True)
    with (
        continuation_clock.authority_guard(scope.project, scope.goal_id, scope.epoch),
        registry.file_lock(Path(registry.HERE) / ("task-graph-" + branch.branch_id + ".lock")),
        SqliteSaver.from_conn_string(str(path)) as saver,
    ):
        return advance_branch(build_branch_flow(host, saver), branch, awaited)


def run_requested(scope: HostRef, stage_id: str, key: str, launch: Launch) -> BranchOutcome:
    """A brain dispatch request opens or reuses this stage's live branch in the round."""
    control_bootstrap.require_graph()
    from model_desk_control.host_branch import BranchOutcome, BranchRef, branch_identity
    from model_desk_control.host_branch_store import BranchError, BranchStatus, open_branch

    requested = BranchRef(
        project=scope.project,
        goal_id=scope.goal_id,
        stage_id=stage_id,
        branch_id=branch_identity(scope.project, scope.goal_id, stage_id, scope.operation_id, key),
        epoch=scope.epoch,
        origin_operation_id=scope.operation_id,
    )
    with continuation_store.mutation(scope.project, scope.goal_id) as database:
        record = open_branch(database, requested)
    if record.status is BranchStatus.DISPATCHED:
        # The branch row is not proof: replay the original receipt, which stays UNKNOWN if lost.
        stage = stage_store.get_stage(stage_id, scope.project)
        if stage is None or launch(stage, key) != record.task_id:
            code = "branch_receipt_mismatch"
            raise BranchError(code)
    if record.status is BranchStatus.DISPATCHED or record.status is BranchStatus.RETIRED:
        return BranchOutcome(
            branch_id=record.reference.branch_id,
            step="closed",
            waiting=False,
            task_id=record.task_id,
        )
    branch = record.reference
    effect_key = key if branch.branch_id == requested.branch_id else "branch:" + branch.branch_id
    return _advance(scope, branch, StageBranchHost(launch, effect_key), record.decision_id)


def resume_waiting(scope: HostRef, launch: Launch) -> tuple[BranchOutcome, ...]:
    """Wake branches whose card is no longer pending; untouched cards cost no graph call."""
    control_bootstrap.require_graph()
    from brain_requests import RequestRejected

    from model_desk_control.host_branch_store import BranchMark, BranchStatus, live, mark

    with continuation_store.readonly(scope.project) as database:
        branches = () if database is None else live(database, scope.goal_id)
    pending = {
        str(row["id"])
        for row in human_decisions.list_decisions(scope.project, scope.goal_id)
        if row.get("status") == "pending"
    }
    outcomes: list[BranchOutcome] = []
    for record in branches:
        # An OPEN branch is still owned by its requesting round's replay and original key.
        if record.status is not BranchStatus.WAITING or (
            record.decision_id in pending and record.reference.epoch == scope.epoch
        ):
            continue
        host = StageBranchHost(launch, "branch:" + record.reference.branch_id)
        try:
            outcomes.append(_advance(scope, record.reference, host, record.decision_id))
        except RequestRejected as refusal:
            # A proven no-effect refusal closes only this branch; the brain sees the reason.
            with continuation_store.mutation(scope.project, scope.goal_id) as database:
                _ = mark(
                    database,
                    record.reference.branch_id,
                    BranchMark(status=BranchStatus.RETIRED, reason=str(refusal)[:1000]),
                )
    return tuple(outcomes)


def listing(project: str, goal_id: str) -> list[Record]:
    """Read-only view for context and panels; it never opens a checkpoint or dispatches."""
    control_bootstrap.bind_source()
    from model_desk_control.host_branch_store import live

    with continuation_store.readonly(project) as database:
        rows = () if database is None else live(database, goal_id)
    return [
        {
            "branch_id": row.reference.branch_id,
            "stage_id": row.reference.stage_id,
            "status": row.status.value,
            "decision_id": row.decision_id,
            "epoch": row.reference.epoch,
        }
        for row in rows
    ]
