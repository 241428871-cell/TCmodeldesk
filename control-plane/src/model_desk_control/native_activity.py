"""Project bounded native tool activity into lightweight message-flow nodes."""

from collections.abc import Sequence
from typing import Final

from model_desk_control.native_events import NativeEvent
from model_desk_control.trace_models import FlowSession, MessageEvent

MAX_ACTIVITY_NODES: Final = 32
SUBAGENT_TITLES: Final = frozenset({"agent", "task", "subagent", "spawn_agent"})


def _label(event: NativeEvent) -> str:
    title = event.title.strip() or "未命名"
    lowered = title.casefold()
    if lowered in SUBAGENT_TITLES or "subagent_type=" in event.detail:
        return ("子代理 · " + title)[:200]
    names = {"tool": "工具", "command": "命令", "file_change": "文件变更"}
    if event.kind == "file_change" and title == names[event.kind]:
        return title
    return (names[event.kind] + " · " + title)[:200]


def project_activity(
    session: FlowSession,
    events: Sequence[NativeEvent],
    *,
    first_sequence: int,
) -> tuple[tuple[FlowSession, ...], tuple[MessageEvent, ...], bool]:
    """Create content-free tool/subagent nodes and observed direction edges."""
    nodes: list[FlowSession] = []
    messages: list[MessageEvent] = []
    by_ref: dict[str, int] = {}
    latest: int | None = None
    sequence = first_sequence
    limited = False
    for index, event in enumerate(events):
        if event.kind in {"tool", "command", "file_change"}:
            if len(nodes) >= MAX_ACTIVITY_NODES:
                limited = True
                continue
            key = f"native:{session.session_key}:{index}"
            complete = event.status in {"completed", "ok", "item.completed"}
            node = FlowSession(
                session_key=key,
                project_id=session.project_id,
                goal_id=session.goal_id,
                model_label=_label(event),
                role="external",
                status="exited" if complete else "unknown",
                view_kind="unavailable",
                stale=not complete,
            )
            nodes.append(node)
            latest = len(nodes) - 1
            if event.ref:
                by_ref[event.ref] = latest
            messages.append(
                MessageEvent(
                    event_id=f"{key}:start",
                    seq=sequence,
                    project_id=session.project_id,
                    goal_id=session.goal_id,
                    source_session=session.session_key,
                    target_session=key,
                    message_id=f"{key}:call",
                    kind="sent",
                    origin="native_event",
                )
            )
            sequence += 1
            if complete:
                messages.append(
                    MessageEvent(
                        event_id=f"{key}:completed",
                        seq=sequence,
                        project_id=session.project_id,
                        goal_id=session.goal_id,
                        source_session=key,
                        target_session=session.session_key,
                        message_id=f"{key}:completed",
                        kind="received",
                        origin="native_event",
                    )
                )
                sequence += 1
            continue
        if event.kind not in {"tool_result", "message"}:
            continue
        owner = by_ref.get(event.parent) if event.parent else latest
        if owner is None:
            continue
        node = nodes[owner]
        if event.kind == "tool_result":
            nodes[owner] = node.model_copy(update={"status": "exited", "stale": False})
        messages.append(
            MessageEvent(
                event_id=f"{node.session_key}:return:{index}",
                seq=sequence,
                project_id=session.project_id,
                goal_id=session.goal_id,
                source_session=node.session_key,
                target_session=session.session_key,
                message_id=f"{node.session_key}:return:{index}",
                kind="received",
                origin="native_event",
            )
        )
        sequence += 1
    return tuple(nodes), tuple(messages), limited
