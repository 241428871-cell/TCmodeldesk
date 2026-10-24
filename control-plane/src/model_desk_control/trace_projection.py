"""Pure bounded projection: no CLI, filesystem, scheduler or network access."""

from collections.abc import Sequence
from typing import Final

from model_desk_control.trace_models import FlowEdge, FlowSession, FlowSnapshot, MessageEvent

MAX_EVENTS: Final = 2000
MAX_SESSIONS: Final = 1000
VISIBLE_NODES: Final = 100
VISIBLE_EDGES: Final = 200


class ProjectionError(ValueError):
    """Malformed observation windows fail explicitly, rather than inventing edges."""

    def __init__(self, code: str) -> None:
        """Retain a safe stable reason without exposing raw native output."""
        self.code: str = code
        super().__init__(code)


def _project_events(project_id: str, events: Sequence[MessageEvent]) -> tuple[MessageEvent, ...]:
    """Reject ambiguous identities while excluding other projects before counting."""
    unique: dict[str, MessageEvent] = {}
    sequence_ids: dict[int, str] = {}
    for event in events:
        if event.project_id != project_id:
            continue
        prior = unique.get(event.event_id)
        if prior is not None and prior != event:
            code = "conflicting_event_identity"
            raise ProjectionError(code)
        if event.seq in sequence_ids and sequence_ids[event.seq] != event.event_id:
            code = "conflicting_aggregate_sequence"
            raise ProjectionError(code)
        unique[event.event_id] = event
        sequence_ids[event.seq] = event.event_id
    return tuple(unique.values())


def project_flow(
    project_id: str,
    sessions: Sequence[FlowSession],
    events: Sequence[MessageEvent],
    *,
    focus_sessions: tuple[str, ...] = (),
) -> FlowSnapshot:
    """Reduce one bounded window, deduplicating messages and isolating projects."""
    if (
        len(events) > MAX_EVENTS
        or len(sessions) > MAX_SESSIONS
        or len(focus_sessions) > VISIBLE_NODES
    ):
        code = "observation_window_exceeds_budget"
        raise ProjectionError(code)
    nodes: dict[str, FlowSession] = {}
    for session in sessions:
        if session.project_id != project_id:
            continue
        prior = nodes.get(session.session_key)
        if prior is not None and prior != session:
            code = "conflicting_session_registration"
            raise ProjectionError(code)
        nodes[session.session_key] = session
    unique = _project_events(project_id, events)
    grouped: dict[tuple[str, str], list[MessageEvent]] = {}
    unresolved: set[tuple[str, str, str]] = set()
    for event in unique:
        source = nodes.get(event.source_session)
        target = nodes.get(event.target_session)
        if (
            source is None
            or target is None
            or source.goal_id != event.goal_id
            or target.goal_id != event.goal_id
        ):
            unresolved.add((event.source_session, event.target_session, event.message_id))
            continue
        grouped.setdefault((source.session_key, target.session_key), []).append(event)
    summaries: list[FlowEdge] = []
    for (source, target), rows in grouped.items():
        latest = max(rows, key=lambda event: event.seq)
        summaries.append(
            FlowEdge(
                source_session=source,
                target_session=target,
                message_count=len({event.message_id for event in rows}),
                sent_count=len({event.message_id for event in rows if event.kind == "sent"}),
                received_count=len(
                    {event.message_id for event in rows if event.kind == "received"}
                ),
                latest_seq=latest.seq,
                latest_event_id=latest.event_id,
            )
        )
    focus = frozenset(focus_sessions)
    ordered_nodes = sorted(
        nodes.values(),
        key=lambda node: (
            node.session_key not in focus,
            node.status not in ("running", "unknown"),
            node.session_key,
        ),
    )
    visible = tuple(ordered_nodes[:VISIBLE_NODES])
    visible_ids = {node.session_key for node in visible}
    visible_edges = sorted(
        (
            edge
            for edge in summaries
            if edge.source_session in visible_ids and edge.target_session in visible_ids
        ),
        key=lambda edge: (-edge.latest_seq, edge.source_session, edge.target_session),
    )[:VISIBLE_EDGES]
    return FlowSnapshot(
        project_id=project_id,
        nodes=visible,
        edges=tuple(visible_edges),
        latest_seq=max((event.seq for event in unique), default=0),
        omitted_nodes=len(nodes) - len(visible),
        omitted_edges=len(summaries) - len(visible_edges),
        unresolved_messages=len(unresolved),
    )
