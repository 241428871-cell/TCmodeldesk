"""Observation projections cannot dispatch, leak bodies or manufacture execution."""

from typing import Literal

import pytest
from pydantic import ValidationError

from model_desk_control.trace_models import FlowSession, MessageEvent
from model_desk_control.trace_projection import MAX_EVENTS, ProjectionError, project_flow


def session(key: str, *, project: str = "p", goal: str = "g") -> FlowSession:
    return FlowSession(
        session_key=key,
        project_id=project,
        goal_id=goal,
        model_label="exact-model",
        role="execution",
        status="registered",
        view_kind="native_events",
    )


def event(
    seq: int,
    *,
    kind: Literal["sent", "received"] = "sent",
    source: str = "a",
    target: str = "b",
    project: str = "p",
) -> MessageEvent:
    return MessageEvent(
        event_id=f"event-{seq}",
        seq=seq,
        project_id=project,
        goal_id="g",
        source_session=source,
        target_session=target,
        message_id="message-1",
        kind=kind,
        origin="native_event",
    )


def test_send_receive_and_duplicate_events_count_one_message() -> None:
    sent = event(1)
    snapshot = project_flow(
        "p", (session("a"), session("b")), (sent, sent, event(2, kind="received"))
    )
    assert len(snapshot.edges) == 1
    edge = snapshot.edges[0]
    assert (edge.message_count, edge.sent_count, edge.received_count) == (1, 1, 1)
    assert snapshot.latest_seq == 2
    assert all(node.status == "registered" for node in snapshot.nodes)


def test_only_received_does_not_invent_send_confirmation() -> None:
    snapshot = project_flow("p", (session("a"), session("b")), (event(1, kind="received"),))
    assert snapshot.edges[0].sent_count == 0
    assert snapshot.edges[0].received_count == 1


def test_feedback_edges_are_valid_but_no_events_means_no_actual_edges() -> None:
    nodes = (session("a"), session("b"))
    assert project_flow("p", nodes, ()).edges == ()
    feedback = project_flow("p", nodes, (event(1), event(2, source="b", target="a")))
    assert len(feedback.edges) == 2


def test_other_projects_do_not_leak_nodes_counts_or_cursor() -> None:
    snapshot = project_flow(
        "p", (session("a"), session("secret", project="other")), (event(900, project="other"),)
    )
    assert [node.session_key for node in snapshot.nodes] == ["a"]
    assert snapshot.edges == ()
    assert snapshot.latest_seq == 0
    assert snapshot.unresolved_messages == 0


def test_unregistered_or_other_goal_endpoint_is_an_observation_gap() -> None:
    snapshot = project_flow("p", (session("a"), session("b", goal="another")), (event(1),))
    assert snapshot.edges == ()
    assert snapshot.unresolved_messages == 1


def test_conflicting_id_or_sequence_is_rejected() -> None:
    with pytest.raises(ProjectionError, match="conflicting_event_identity"):
        _ = project_flow("p", (), (event(1), event(1, target="different")))
    with pytest.raises(ProjectionError, match="conflicting_aggregate_sequence"):
        _ = project_flow("p", (), (event(1), event(1).model_copy(update={"event_id": "other"})))


def test_render_limits_report_omissions_without_creating_an_edge_to_hidden_node() -> None:
    nodes = tuple(session(f"node-{index:03}") for index in range(101))
    snapshot = project_flow("p", nodes, (event(1, source="node-000", target="node-100"),))
    assert len(snapshot.nodes) == 100
    assert snapshot.omitted_nodes == 1
    assert snapshot.omitted_edges == 1
    assert snapshot.edges == ()


def test_window_is_bounded_and_raw_output_cannot_enter_event_schema() -> None:
    with pytest.raises(ProjectionError, match="observation_window_exceeds_budget"):
        _ = project_flow("p", (), (event(1),) * (MAX_EVENTS + 1))
    raw = event(1).model_dump()
    raw["terminal_body"] = "private output"
    with pytest.raises(ValidationError, match="extra_forbidden"):
        _ = MessageEvent.model_validate(raw)


def test_focused_hidden_node_can_be_inspected_without_increasing_render_budget() -> None:
    nodes = tuple(session(f"node-{index:03}") for index in range(101))
    snapshot = project_flow("p", nodes, (), focus_sessions=("node-100",))
    assert snapshot.nodes[0].session_key == "node-100"
    assert len(snapshot.nodes) == 100
    assert snapshot.omitted_nodes == 1


def test_actual_running_node_is_visible_before_idle_registrations() -> None:
    idle = tuple(session(f"node-{index:03}") for index in range(100))
    running = session("node-100").model_copy(update={"status": "running"})
    snapshot = project_flow("p", (*idle, running), ())
    assert snapshot.nodes[0] == running
    assert len(snapshot.nodes) == 100
