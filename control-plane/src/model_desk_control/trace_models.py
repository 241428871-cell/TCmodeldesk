"""Content-free message-flow read models, separate from execution authority."""

from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

Identity = Annotated[str, Field(min_length=1, max_length=200)]
GoalIdentity = Annotated[str, Field(max_length=200)]


class FlowRecord(BaseModel):
    """Immutable transport records reject undeclared output or secret fields."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="forbid")


class FlowSession(FlowRecord):
    """Registry facts; a model connection alone cannot create a running session."""

    session_key: Identity
    project_id: Identity
    goal_id: GoalIdentity
    model_label: Identity
    role: Literal["decision", "execution", "external"]
    status: Literal["registered", "idle", "running", "exited", "unknown"]
    view_kind: Literal["pty_mirror", "native_events", "unavailable"]
    stale: bool = False


class MessageEvent(FlowRecord):
    """One observed event; sender/receiver evidence is never inferred from a plan."""

    event_id: Identity
    seq: Annotated[int, Field(ge=1)]
    project_id: Identity
    goal_id: GoalIdentity
    source_session: Identity
    target_session: Identity
    message_id: Identity
    kind: Literal["sent", "received"]
    origin: Literal["control_dispatch", "native_event", "context_snapshot"]


class FlowEdge(FlowRecord):
    """Bounded edge summaries do not carry native messages or terminal bodies."""

    source_session: Identity
    target_session: Identity
    message_count: int
    sent_count: int
    received_count: int
    latest_seq: int
    latest_event_id: Identity


class FlowSnapshot(FlowRecord):
    """One project projection with explicit omissions and observation gaps."""

    project_id: Identity
    nodes: tuple[FlowSession, ...]
    edges: tuple[FlowEdge, ...]
    latest_seq: int
    omitted_nodes: int
    omitted_edges: int
    unresolved_messages: int
