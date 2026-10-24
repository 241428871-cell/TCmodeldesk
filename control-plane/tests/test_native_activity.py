from model_desk_control.native_activity import MAX_ACTIVITY_NODES, project_activity
from model_desk_control.native_events import NativeEvent
from model_desk_control.trace_models import FlowSession


def test_tool_and_subagent_calls_become_content_free_nodes_and_edges() -> None:
    session = FlowSession(
        session_key="task:t1",
        project_id="/project",
        goal_id="g1",
        model_label="Claude",
        role="execution",
        status="running",
        view_kind="native_events",
    )
    nodes, messages, limited = project_activity(
        session,
        (
            NativeEvent(
                kind="tool",
                title="Task",
                detail="subagent_type=Explore",
                ref="toolu_1",
            ),
            NativeEvent(
                kind="message",
                title="回复",
                detail="private output is never copied to the flow",
                parent="toolu_1",
            ),
            NativeEvent(
                kind="tool_result",
                title="工具结果",
                detail="private result is never copied to the flow",
                parent="toolu_1",
                status="ok",
            ),
        ),
        first_sequence=11,
    )
    assert not limited
    assert len(nodes) == 1
    assert nodes[0].model_label == "子代理 · Task"
    assert nodes[0].status == "exited"
    assert [(row.source_session, row.target_session) for row in messages] == [
        ("task:t1", nodes[0].session_key),
        (nodes[0].session_key, "task:t1"),
        (nodes[0].session_key, "task:t1"),
    ]
    dumped = str((nodes, messages))
    assert "private output" not in dumped
    assert "private result" not in dumped


def test_completed_codex_item_has_a_return_edge_without_waiting_for_another_event() -> None:
    session = FlowSession(
        session_key="task:t1",
        project_id="/project",
        goal_id="g1",
        model_label="Codex",
        role="execution",
        status="exited",
        view_kind="native_events",
    )
    nodes, messages, limited = project_activity(
        session,
        (NativeEvent(kind="command", title="pytest -q", status="completed"),),
        first_sequence=3,
    )
    assert not limited
    assert nodes[0].status == "exited"
    assert [(row.source_session, row.target_session, row.kind) for row in messages] == [
        ("task:t1", nodes[0].session_key, "sent"),
        (nodes[0].session_key, "task:t1", "received"),
    ]


def test_activity_projection_is_bounded_and_file_change_label_is_not_repeated() -> None:
    session = FlowSession(
        session_key="task:t1",
        project_id="/project",
        goal_id="g1",
        model_label="Codex",
        role="execution",
        status="exited",
        view_kind="native_events",
    )
    nodes, messages, limited = project_activity(
        session,
        tuple(
            NativeEvent(kind="file_change", title="文件变更", status="completed")
            for _ in range(MAX_ACTIVITY_NODES + 5)
        ),
        first_sequence=1,
    )
    assert limited
    assert len(nodes) == MAX_ACTIVITY_NODES
    assert all(row.model_label == "文件变更" for row in nodes)
    assert len(messages) == MAX_ACTIVITY_NODES * 2
