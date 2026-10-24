"""Per-stage task graphs whose human waits are durable interrupts, not a paused goal."""

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from json import dumps
from typing import TYPE_CHECKING, Annotated, Literal, Protocol, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, interrupt
from pydantic import Field

from model_desk_control.host_flow import HostRecord

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

BRANCH_ID: str = r"^[a-f0-9]{64}$"


class BranchGate(StrEnum):
    """Live domain observations; the checkpoint never stores dispatch authorization."""

    READY = "ready"
    WAITING = "waiting"
    BLOCKED = "blocked"
    REVOKED = "revoked"
    SETTLED = "settled"


class BranchRef(HostRecord):
    """Trusted identity of one work branch; stage content stays in the domain store."""

    project: Annotated[str, Field(min_length=1)]
    goal_id: Annotated[str, Field(min_length=1, max_length=100)]
    stage_id: Annotated[str, Field(min_length=1, max_length=100)]
    branch_id: Annotated[str, Field(pattern=BRANCH_ID)]
    epoch: Annotated[int, Field(ge=0)]
    origin_operation_id: Annotated[str, Field(min_length=1, max_length=100)]


class BranchObservation(HostRecord):
    """One read of current authority, plan and human exceptions for this stage."""

    gate: BranchGate
    decision_id: Annotated[str, Field(max_length=100)] = ""
    reason: Annotated[str, Field(max_length=1000)] = ""


class BranchWake(HostRecord):
    """A wakeup names the awaited card; it carries no answer and grants nothing."""

    decision_id: Annotated[str, Field(min_length=1, max_length=100)]


class BranchHost(Protocol):
    """The host owns every effect, its idempotency and its current authority checks."""

    def observe(self, reference: BranchRef) -> BranchObservation:
        """Read the live domain without writing or starting work."""
        ...

    def park(self, reference: BranchRef, decision_id: str) -> None:
        """Record idempotently that this branch alone waits for one human card."""
        ...

    def dispatch(self, reference: BranchRef) -> str:
        """Dispatch once through the original receipts; return the task or an empty skip."""
        ...

    def retire(self, reference: BranchRef, gate: BranchGate, reason: str) -> None:
        """Close the branch without an effect; later rounds may open a new one."""
        ...


BranchStep = Literal["", "ready", "waiting", "woken", "recheck", "dispatched", "closed"]


class BranchState(TypedDict):
    """Compact position and references; the domain store remains authoritative."""

    reference_json: str
    step: BranchStep
    decision_id: str
    task_id: str


class BranchUpdate(TypedDict, total=False):
    """A node changes its position, never the trusted branch identity."""

    step: BranchStep
    decision_id: str
    task_id: str


def branch_identity(project: str, goal_id: str, stage_id: str, operation_id: str, key: str) -> str:
    """Encode the requesting round and action position without ambiguous separators."""
    encoded = dumps(
        (project, goal_id, stage_id, operation_id, key),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return sha256(encoded.encode()).hexdigest()


def thread_id(reference: BranchRef) -> str:
    """Each branch has its own checkpoint thread, separate from the goal round."""
    return "native-task-v1:" + reference.branch_id


def initial_state(reference: BranchRef) -> BranchState:
    """Start at the live gate; nothing in the input authorizes a dispatch."""
    return {
        "reference_json": reference.model_dump_json(),
        "step": "",
        "decision_id": "",
        "task_id": "",
    }


@dataclass(frozen=True, slots=True)
class BranchNodes:
    """Compilation stores the host only; no node runs until the graph is invoked."""

    host: BranchHost

    def _close(self, reference: BranchRef, observed: BranchObservation) -> BranchUpdate:
        if observed.gate is not BranchGate.SETTLED:
            self.host.retire(reference, observed.gate, observed.reason)
        return {"step": "closed"}

    def gate(self, state: BranchState) -> BranchUpdate:
        """Observe live facts; only this read decides waiting, dispatch or closure."""
        reference = BranchRef.model_validate_json(state["reference_json"])
        observed = self.host.observe(reference)
        match observed.gate:
            case BranchGate.WAITING:
                self.host.park(reference, observed.decision_id)
                return {"step": "waiting", "decision_id": observed.decision_id}
            case BranchGate.READY:
                return {"step": "ready"}
            case BranchGate.BLOCKED | BranchGate.REVOKED | BranchGate.SETTLED:
                return self._close(reference, observed)

    def wait(self, state: BranchState) -> BranchUpdate:
        """Re-entry only re-reads the reference; the answer stays in the domain store."""
        _ = interrupt(state["decision_id"], response_schema=BranchWake)
        return {"step": "woken"}

    def dispatch(self, state: BranchState) -> BranchUpdate:
        """A restored ready checkpoint is rechecked before the original dispatch entrypoint."""
        reference = BranchRef.model_validate_json(state["reference_json"])
        if self.host.observe(reference).gate is not BranchGate.READY:
            return {"step": "recheck"}
        return {"step": "dispatched", "task_id": self.host.dispatch(reference)}


def _after_gate(state: BranchState) -> Literal["wait", "dispatch", "__end__"]:
    match state["step"]:
        case "waiting":
            return "wait"
        case "ready":
            return "dispatch"
        case _:
            return "__end__"


def _after_dispatch(state: BranchState) -> Literal["gate", "__end__"]:
    return "gate" if state["step"] == "recheck" else "__end__"


def build_branch_flow(
    host: BranchHost, saver: BaseCheckpointSaver[str]
) -> CompiledStateGraph[BranchState, None, BranchState, BranchState]:
    """Compile one stage branch; a waiting branch never holds back its siblings."""
    nodes = BranchNodes(host)
    graph = StateGraph(BranchState)
    _ = graph.add_node("gate", nodes.gate)
    _ = graph.add_node("wait", nodes.wait)
    _ = graph.add_node("dispatch", nodes.dispatch)
    _ = graph.add_edge(START, "gate")
    _ = graph.add_conditional_edges("gate", _after_gate)
    _ = graph.add_edge("wait", "gate")
    _ = graph.add_conditional_edges("dispatch", _after_dispatch)
    return graph.compile(checkpointer=saver)


type BranchFlow = CompiledStateGraph[BranchState, None, BranchState, BranchState]


class BranchOutcome(HostRecord):
    """Where one invocation stopped; a waiting branch is a normal, resumable result."""

    branch_id: Annotated[str, Field(pattern=BRANCH_ID)]
    step: BranchStep
    waiting: bool
    decision_id: Annotated[str, Field(max_length=100)] = ""
    task_id: Annotated[str, Field(max_length=300)] = ""


def advance_branch(flow: BranchFlow, reference: BranchRef, awaited: str = "") -> BranchOutcome:
    """Resume a saved interrupt, finish an interrupted step, or start at the live gate."""
    config: RunnableConfig = {"configurable": {"thread_id": thread_id(reference)}}
    saved = flow.get_state(config)
    if saved.interrupts and awaited:
        wake = BranchWake(decision_id=awaited)
        output = flow.invoke(Command[str](resume=wake.model_dump()), config, version="v2")
    elif saved.next and not saved.interrupts:
        output = flow.invoke(None, config, version="v2")
    else:
        output = flow.invoke(initial_state(reference), config, version="v2")
    value = output.value
    return BranchOutcome(
        branch_id=reference.branch_id,
        step=value["step"],
        waiting=bool(output.interrupts),
        decision_id=value["decision_id"],
        task_id=value["task_id"],
    )
