"""Persistent phases around the workbench's existing native goal authority."""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, ClassVar, Literal, Protocol, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from model_desk_control.native_commands import NativeMode


class HostStep(StrEnum):
    """Stable mechanisms; actual stages and actions remain AI-generated domain data."""

    SNAPSHOT = "snapshot"
    NATIVE = "native"
    ACTIONS = "actions"
    SETTLE = "settle"


class HostRecord(BaseModel):
    """Strict immutable boundaries for legacy-host integration."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="forbid")


class HostRef(HostRecord):
    """Trusted identity of an already claimed intent, not a new scheduling request."""

    project: Annotated[str, Field(min_length=1)]
    goal_id: Annotated[str, Field(min_length=1, max_length=100)]
    operation_id: Annotated[str, Field(min_length=1, max_length=100)]
    epoch: Annotated[int, Field(ge=0)]
    owner_id: Annotated[str, Field(min_length=1)]


class HostProfile(HostRecord):
    """Retain the existing role binding's immutable native options."""

    effort: str = "default"
    fast: bool = False
    context_window: int | None = None
    autocompact_tokens: int | None = None


class HostSnapshot(HostRecord):
    """Full legacy context remains in its domain journal, outside the checkpoint."""

    context: dict[str, JsonValue]
    engine: str
    model: str
    runtime: HostProfile
    directory: str
    session_id: str
    permission_mode: NativeMode
    contract_revision: int = Field(ge=0)
    action_receipts_version: Literal[0, 2] = 0
    plan_revision: int | None = Field(default=None, ge=0)
    settle_receipts_version: Literal[0, 2] = 0


class HostNativeResult(HostRecord):
    """The original validator parses actions again before applying them."""

    actions_json: str
    native_session_id: str
    receipt: str


class HostApplied(HostRecord):
    """Result of the existing shared dispatch and independent acceptance core."""

    dispatched_task_ids: tuple[str, ...]
    goal_completed: bool
    pending_obligation_ids: tuple[str, ...]
    summary: str
    superseded: bool = False


@dataclass(frozen=True, slots=True)
class HostCallbacks:
    """Legacy boundary serializes each phase result instead of exposing mutable globals."""

    snapshot: Callable[[], str]
    native: Callable[[], str]
    actions: Callable[[], str]
    settle: Callable[[], str]


class HostSteps(Protocol):
    """The host supplies current authority and domain idempotency for every effect."""

    def run_step(self, step: HostStep, reference: HostRef) -> None:
        """Execute once or return a durable receipt; unresolved effects must not restart."""
        ...


class HostState(TypedDict):
    """Checkpoints contain compact scope and phase references, never native transcripts."""

    reference_json: str
    last_step: str


class HostUpdate(TypedDict):
    """A phase receipt records position, not fresh dispatch authorization."""

    last_step: str


@dataclass(frozen=True, slots=True)
class HostNodes:
    """Compilation does not call the host or touch its domain repository."""

    host: HostSteps

    def snapshot(self, state: HostState) -> HostUpdate:
        """Capture full authorized inputs through the host's durable journal."""
        self.host.run_step(HostStep.SNAPSHOT, HostRef.model_validate_json(state["reference_json"]))
        return {"last_step": HostStep.SNAPSHOT.value}

    def native(self, state: HostState) -> HostUpdate:
        """Start one exact native operation or use its existing receipt."""
        self.host.run_step(HostStep.NATIVE, HostRef.model_validate_json(state["reference_json"]))
        return {"last_step": HostStep.NATIVE.value}

    def actions(self, state: HostState) -> HostUpdate:
        """Apply proposals through the existing project, fee and acceptance gates."""
        self.host.run_step(HostStep.ACTIONS, HostRef.model_validate_json(state["reference_json"]))
        return {"last_step": HostStep.ACTIONS.value}

    def settle(self, state: HostState) -> HostUpdate:
        """Settle the original intent; a turn ending is not goal completion."""
        self.host.run_step(HostStep.SETTLE, HostRef.model_validate_json(state["reference_json"]))
        return {"last_step": HostStep.SETTLE.value}


def build_host_flow(
    host: HostSteps, saver: BaseCheckpointSaver[str]
) -> CompiledStateGraph[HostState, None, HostState, HostState]:
    """Wrap the existing authority with persistent phases and no second scheduler."""
    nodes = HostNodes(host)
    graph = StateGraph(HostState)
    _ = graph.add_node("snapshot", nodes.snapshot)
    _ = graph.add_node("native", nodes.native)
    _ = graph.add_node("actions", nodes.actions)
    _ = graph.add_node("settle", nodes.settle)
    _ = graph.add_edge(START, "snapshot")
    _ = graph.add_edge("snapshot", "native")
    _ = graph.add_edge("native", "actions")
    _ = graph.add_edge("actions", "settle")
    _ = graph.add_edge("settle", END)
    return graph.compile(checkpointer=saver)
