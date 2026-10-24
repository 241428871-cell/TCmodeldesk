"""A mutable native-runner ledger simulates effects without accounts or model calls."""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, assert_never

if TYPE_CHECKING:
    from collections.abc import Callable

from model_desk_control.outbox import ContinuationLease, OutboxStore
from model_desk_control.plans import PlanPatch
from model_desk_control.round_models import BrainFeedback, FeedbackKind, RoundRequest
from model_desk_control.round_store import RoundStore
from model_desk_control.store import ControlStore
from tests.domain_fixtures import domain_at, task


@dataclass(frozen=True, slots=True)
class FixedClock:
    value: int = 100

    def now(self) -> int:
        return self.value


def feedback_for(
    request: RoundRequest, kind: FeedbackKind = FeedbackKind.RESULT, sequence: int = 1
) -> BrainFeedback:
    patch: PlanPatch | None = None
    reason: str | None = None
    match kind:
        case FeedbackKind.RESULT:
            patch = PlanPatch(
                base_revision=request.plan.revision,
                reason="Native planning result from an isolated fake",
                added=(task("planned-result"),),
            )
        case FeedbackKind.RUNNING:
            pass
        case FeedbackKind.UNKNOWN:
            reason = "Runner observation cannot establish whether the native turn started"
        case FeedbackKind.FAILED:
            reason = "A terminal native failure was observed"
        case FeedbackKind.NOT_STARTED:
            reason = "Fixture runner positively attests no native prompt was sent"
        case _:
            assert_never(kind)
    return BrainFeedback(
        operation_id=request.operation_id,
        receipt_id=f"receipt-{sequence}",
        sequence=sequence,
        kind=kind,
        evidence_ref=f"fixture:runner-observation-{sequence}",
        patch=patch,
        reason=reason,
    )


class SimulatedTransportLossError(RuntimeError):
    """The fake accepted the operation but its transport reply did not arrive."""


class LedgerRunner:
    """Mutable fake ledger records native effects separately from controller checkpoints."""

    def __init__(self, *, immediate_result: bool = True) -> None:
        self.starts: list[RoundRequest] = []
        self.inspections: list[RoundRequest] = []
        self.ledger: dict[str, BrainFeedback] = {}
        self.immediate_result: bool = immediate_result
        self.lose_first_reply: bool = False
        self.on_start: Callable[[], None] | None = None

    def start_round(self, request: RoundRequest) -> BrainFeedback:
        self.starts.append(request)
        kind = FeedbackKind.RESULT if self.immediate_result else FeedbackKind.RUNNING
        feedback = feedback_for(request, kind)
        self.ledger[request.operation_id] = feedback
        if self.on_start is not None:
            self.on_start()
        if self.lose_first_reply:
            self.lose_first_reply = False
            reason = "simulated transport loss after native acceptance"
            raise SimulatedTransportLossError(reason)
        return feedback

    def inspect_round(self, request: RoundRequest) -> BrainFeedback:
        self.inspections.append(request)
        return self.ledger.get(request.operation_id, feedback_for(request, FeedbackKind.UNKNOWN))


def leased_goal(directory: Path) -> tuple[ControlStore, ContinuationLease]:
    controls = domain_at(directory)
    _ = controls.claim(goal_id="project/goal", request_id="tick", epoch=0, revision=0, now=100)
    lease = OutboxStore(controls).take(
        goal_id="project/goal", owner="runner-instance-1", now=100, lease_seconds=30
    )
    assert lease is not None
    return controls, lease


def prepared_round(directory: Path) -> tuple[ControlStore, ContinuationLease, RoundStore]:
    controls, lease = leased_goal(directory)
    rounds = RoundStore(controls)
    assert rounds.prepare(lease, now=100) is not None
    return controls, lease, rounds
