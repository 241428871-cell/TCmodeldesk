"""A real persistent goal planning graph uses domain authority and one existing outbox."""

from dataclasses import dataclass
from typing import Literal, Protocol, TypedDict, assert_never

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from model_desk_control.database import StoreError
from model_desk_control.lease_rules import ContinuationLease
from model_desk_control.receipts import fingerprint
from model_desk_control.round_models import (
    BrainFeedback,
    FeedbackKind,
    PlanningRound,
    RoundAction,
    RoundRequest,
    RoundStage,
)


class Clock(Protocol):
    """Production and tests supply time explicitly; compilation starts no timer."""

    def now(self) -> int:
        """Return Unix seconds for the immediate authority check."""
        ...


class BrainRunner(Protocol):
    """Native adapters keep their own exact session/turn and runner evidence.

    inspect_round must never send a new prompt. NOT_STARTED needs positive proof;
    absence of a file, process or an observation timeout is UNKNOWN.
    """

    def start_round(self, request: RoundRequest) -> BrainFeedback:
        """Start only after the domain arm succeeds; preserve this exact operation ID."""
        ...

    def inspect_round(self, request: RoundRequest) -> BrainFeedback:
        """Observe the original operation without starting, resuming or replacing a CLI turn."""
        ...


class PlanningRounds(Protocol):
    """An adapter may reuse an existing goal authority instead of adding a second clock."""

    def prepare(self, lease: ContinuationLease, *, now: int) -> PlanningRound | None:
        """Persist or recover an exact planning input under the existing authority."""
        ...

    def choose(self, lease: ContinuationLease, *, now: int) -> tuple[RoundAction, PlanningRound]:
        """Atomically select START once; unresolved operations select inspection."""
        ...

    def authorize(self, lease: ContinuationLease, *, now: int) -> bool:
        """Check current ownership immediately before the native handoff."""
        ...

    def observe(self, goal_id: str, feedback: BrainFeedback) -> PlanningRound:
        """Store a trusted receipt without granting new dispatch authority."""
        ...

    def finalize(self, lease: ContinuationLease) -> PlanningRound:
        """Atomically settle a terminal operation and its plan mutation."""
        ...


GoalPhase = Literal["pending", "prepared", "observed", "waiting", "finalized", "revoked"]


class GoalGraphState(TypedDict):
    """Compact references; a checkpoint is never source authority for new dispatch."""

    lease_json: str
    phase: GoalPhase
    round_revision: int | None


class GoalGraphUpdate(TypedDict, total=False):
    """Nodes update observations without changing the operation identity."""

    phase: GoalPhase
    round_revision: int | None


def initial_goal_state(lease: ContinuationLease) -> GoalGraphState:
    """Initialize one bounded event-driven pass over an existing continuation intent."""
    return {"lease_json": lease.model_dump_json(), "phase": "pending", "round_revision": None}


@dataclass(frozen=True, slots=True)
class GoalNodes:
    """Explicit dependencies keep graph import and compilation inert."""

    rounds: PlanningRounds
    runner: BrainRunner
    clock: Clock

    def prepare(self, state: GoalGraphState) -> GoalGraphUpdate:
        """Persist the original input before any call to a native adapter."""
        lease = ContinuationLease.model_validate_json(state["lease_json"])
        record = self.rounds.prepare(lease, now=self.clock.now())
        if record is None:
            return {"phase": "revoked"}
        return {"phase": "prepared", "round_revision": record.revision}

    def observe(self, state: GoalGraphState) -> GoalGraphUpdate:
        """One first start or one read-only reconciliation; never an automatic resend."""
        lease = ContinuationLease.model_validate_json(state["lease_json"])
        action, record = self.rounds.choose(lease, now=self.clock.now())
        feedback: BrainFeedback | None = None
        match action:
            case RoundAction.START:
                if not self.rounds.authorize(lease, now=self.clock.now()):
                    # This branch is the sole START winner and has not called the runner.
                    proof = fingerprint(lease.goal_id, lease.operation_id)
                    feedback = BrainFeedback(
                        operation_id=lease.operation_id,
                        receipt_id=f"controller-unsent:{proof}",
                        sequence=1,
                        kind=FeedbackKind.NOT_STARTED,
                        evidence_ref=f"controller:before-native-handoff:{proof}",
                        reason="Authority revoked before this controller handed work to the runner",
                    )
                else:
                    feedback = self.runner.start_round(record.request)
            case RoundAction.INSPECT:
                feedback = self.runner.inspect_round(record.request)
            case RoundAction.NONE:
                pass
            case RoundAction.REVOKED:
                return {"phase": "revoked", "round_revision": record.revision}
            case _:
                assert_never(action)
        if feedback is not None:
            if feedback.operation_id != lease.operation_id:
                code = "runner_operation_mismatch"
                raise StoreError(code)
            record = self.rounds.observe(lease.goal_id, feedback)
        return {"phase": "observed", "round_revision": record.revision}

    def settle(self, state: GoalGraphState) -> GoalGraphUpdate:
        """A terminal planning turn may settle; the user's complete goal remains open."""
        lease = ContinuationLease.model_validate_json(state["lease_json"])
        record = self.rounds.finalize(lease)
        return {
            "phase": "finalized" if record.stage is RoundStage.FINALIZED else "waiting",
            "round_revision": record.revision,
        }


def _prepared(state: GoalGraphState) -> Literal["observe", "__end__"]:
    return "observe" if state["phase"] == "prepared" else "__end__"


def build_goal_graph(
    nodes: GoalNodes, checkpointer: BaseCheckpointSaver[str]
) -> CompiledStateGraph[GoalGraphState, None, GoalGraphState, GoalGraphState]:
    """Compile one bounded planning round without a scheduler, provider call or domain I/O."""
    graph = StateGraph(GoalGraphState)
    _ = graph.add_node("prepare", nodes.prepare)
    _ = graph.add_node("observe", nodes.observe)
    _ = graph.add_node("settle", nodes.settle)
    _ = graph.add_edge(START, "prepare")
    _ = graph.add_conditional_edges("prepare", _prepared)
    _ = graph.add_edge("observe", "settle")
    _ = graph.add_edge("settle", END)
    return graph.compile(checkpointer=checkpointer)
