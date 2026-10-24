"""Project-owned flow from actual journal receipts; GET never initializes storage."""

from collections.abc import Callable, Sequence
from contextlib import closing
from pathlib import Path
from sqlite3 import Connection, connect
from typing import Final, Literal

from pydantic import TypeAdapter, ValidationError

from model_desk_control.database import text_cursor
from model_desk_control.host_flow import HostProfile
from model_desk_control.host_messages import round_messages
from model_desk_control.host_trace_models import (
    HostTrace,
    IntentObservation,
    LegacyObservation,
    NativeReference,
    RecordId,
    RoundMessages,
    TaskObservation,
)
from model_desk_control.native_activity import project_activity
from model_desk_control.native_events import parse_events
from model_desk_control.native_output import (
    NativeReadError,
    output_tail,
    owned_path,
    read_metadata,
)
from model_desk_control.trace_models import FlowSession, MessageEvent
from model_desk_control.trace_projection import MAX_EVENTS, project_flow

ROUND_LIMIT: Final = 50
TASK_LIMIT: Final = 500
READ_BUDGET: Final = 2 * 1024 * 1024
NATIVE_STREAM_LIMIT: Final = 16
NATIVE_NODE_LIMIT: Final = 80
type ProcessProbe = Callable[[int, float], Literal["running", "exited", "unknown"]]
IDS: Final = TypeAdapter(list[RecordId])


class SnapshotObservation(LegacyObservation):
    """Only non-secret launch metadata enters the flow response."""

    engine: str = ""
    model: str = ""
    runtime: HostProfile | None = None


def _sessions(
    project: Path, database: Connection, probe: ProcessProbe | None
) -> tuple[list[FlowSession], list[NativeReference], int]:
    rows = text_cursor(
        database, "SELECT id, goal_id FROM tasks ORDER BY rowid DESC LIMIT 501"
    ).fetchall()
    nodes: list[FlowSession] = []
    references: list[NativeReference] = []
    gaps = max(0, len(rows) - TASK_LIMIT)
    remaining = READ_BUDGET
    for task_id, goal_id in rows[:TASK_LIMIT]:
        try:
            _ = IDS.validate_python([task_id, *([goal_id] if goal_id else [])])
            path = owned_path(project, Path(".model-desk/runs") / task_id / "task.json")
            if path.stat().st_size > remaining:
                gaps += 1
                continue
            raw = read_metadata(path)
            remaining -= len(raw.encode())
            task = TaskObservation.model_validate_json(raw)
        except (OSError, UnicodeError, ValidationError, NativeReadError):
            gaps += 1
            continue
        if task.id != task_id or task.project != str(project) or (task.goal_id or "") != goal_id:
            gaps += 1
            continue
        key = "task:" + task.id
        active = task.status in {"running", "recovery_pending"}
        status = (
            probe(task.process_pid, task.process_birth)
            if (
                probe is not None
                and task.process_pid is not None
                and task.process_birth is not None
            )
            else "unknown"
            if active
            else "exited"
        )
        captured = task.native_output_capture == 1 and task.engine in {"claude_cli", "codex_cli"}
        nodes.append(
            FlowSession(
                session_key=key,
                project_id=str(project),
                goal_id=goal_id,
                model_label=(task.model or task.model_id or task.engine or "执行端")[:200],
                role="execution",
                status="registered" if task.status == "queued" else status,
                view_kind="native_events" if captured else "unavailable",
                stale=status == "unknown",
            )
        )
        references.append(
            NativeReference(
                session_key=key,
                record_id=task.id,
                goal_id=goal_id,
                role="execution",
                model_id=task.model_id,
                engine=task.engine,
                native_session_id=task.native_session_id,
                permission_mode=task.cli_permission_mode or task.sandbox_mode,
                operation_status=task.status,
                native_capture_id=task.native_capture_id,
                runtime=task.cli_runtime,
            )
        )
    return nodes, references, gaps


def _rounds(database: Connection) -> list[Sequence[str]]:
    return text_cursor(
        database,
        """
        SELECT CAST(i.rowid AS TEXT), i.id, i.goal_id, i.data,
          json_object('engine',COALESCE(json_extract(s.result_json,'$.engine'),''),
            'model',COALESCE(json_extract(s.result_json,'$.model'),''),
            'runtime',json_extract(s.result_json,'$.runtime')),
          COALESCE(json_extract(s.result_json,'$.context.tasks'),'[]'),
          COALESCE(CASE WHEN a.status='done' THEN a.result_json END,''),
          COALESCE(n.status,''), COALESCE(a.status,'')
        FROM (SELECT rowid AS seq, * FROM continuation_intents ORDER BY rowid DESC LIMIT 51) r
        JOIN continuation_intents i ON i.id=r.id
        LEFT JOIN goal_graph_steps s ON s.intent_id=i.id AND s.step='snapshot' AND s.status='done'
        LEFT JOIN goal_graph_steps a ON a.intent_id=i.id AND a.step='actions'
        LEFT JOIN goal_graph_steps n ON n.intent_id=i.id AND n.step='native'
        ORDER BY r.seq DESC
        """,
    ).fetchall()


def _native_output_path(reference: NativeReference) -> Path:
    if reference.role == "decision":
        return (
            Path(".model-desk/goal-brain")
            / reference.goal_id
            / reference.record_id
            / "native-output.jsonl"
        )
    root = Path(".model-desk/runs") / reference.record_id
    if reference.native_capture_id:
        root = root / "native-cli" / reference.native_capture_id
    return root / "native-output.txt"


def _native_activity(
    project: Path,
    sessions: list[FlowSession],
    references: list[NativeReference],
    events: list[MessageEvent],
) -> tuple[int, bool]:
    """Read a small recent native-output window and add content-free activity nodes."""
    session_by_key = {row.session_key: row for row in sessions}
    candidates = [
        row
        for row in references
        if session_by_key.get(row.session_key) is not None
        and session_by_key[row.session_key].view_kind == "native_events"
    ]
    candidates.sort(
        key=lambda row: session_by_key[row.session_key].status not in {"running", "unknown"}
    )
    limited = len(candidates) > NATIVE_STREAM_LIMIT
    gaps = 0
    sequence = max((row.seq for row in events), default=0) + 1
    added = 0
    for reference in candidates[:NATIVE_STREAM_LIMIT]:
        try:
            tail = output_tail(project, _native_output_path(reference), "stdout")
        except (OSError, UnicodeError, NativeReadError):
            gaps += 1
            continue
        if not tail.available:
            continue
        parsed = parse_events(tail.text, partial_first_line=tail.omitted_bytes > 0)
        new_nodes, new_events, stream_limited = project_activity(
            session_by_key[reference.session_key], parsed.events, first_sequence=sequence
        )
        room = max(0, NATIVE_NODE_LIMIT - added)
        if len(new_nodes) > room:
            new_nodes = new_nodes[:room]
            allowed = {row.session_key for row in new_nodes}
            new_events = tuple(
                row
                for row in new_events
                if row.source_session in allowed or row.target_session in allowed
            )
            limited = True
        sessions.extend(new_nodes)
        events.extend(new_events)
        added += len(new_nodes)
        sequence = max((row.seq for row in new_events), default=sequence - 1) + 1
        gaps += parsed.skipped_lines
        limited = limited or stream_limited or parsed.truncated
        if added >= NATIVE_NODE_LIMIT:
            limited = limited or len(candidates) > 1
            break
    return gaps, limited


def _read(project: Path, database: Connection, probe: ProcessProbe | None) -> HostTrace:
    tables = {
        row[0]
        for row in text_cursor(
            database, "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    nodes, references, gaps = (
        _sessions(project, database, probe) if "tasks" in tables else ([], [], 0)
    )
    events: list[MessageEvent] = []
    rows = _rounds(database) if {"continuation_intents", "goal_graph_steps"} <= tables else []
    for (
        seq,
        intent_id,
        goal_id,
        raw,
        launch,
        context,
        actions,
        native_status,
        action_status,
    ) in rows[:ROUND_LIMIT]:
        try:
            intent = IntentObservation.model_validate_json(raw)
            if intent.id != intent_id or intent.goal_id != goal_id:
                gaps += 1
                continue
            snapshot = SnapshotObservation.model_validate_json(launch)
            observed = round_messages(
                str(project),
                intent,
                RoundMessages(int(seq), actions, context, native_status == "done"),
            )
        except (ValidationError, NativeReadError):
            gaps += 1
            continue
        events.extend(observed)
        gaps += int(native_status == "inflight") + int(action_status == "inflight")
        key = "brain:" + intent.id
        status = (
            probe(intent.pid, intent.birth)
            if (probe is not None and intent.pid is not None and intent.birth is not None)
            else "exited"
            if native_status == "done"
            else "unknown"
        )
        output = owned_path(
            project,
            Path(".model-desk/goal-brain") / intent.goal_id / intent.id / "native-output.jsonl",
        )
        nodes.append(
            FlowSession(
                session_key=key,
                project_id=str(project),
                goal_id=intent.goal_id,
                model_label=(snapshot.model or intent.brain_model_id or "主脑")[:200],
                role="decision",
                status="registered" if intent.status == "claimed" else status,
                stale=status == "unknown",
                view_kind="native_events" if output.is_file() else "unavailable",
            )
        )
        references.append(
            NativeReference(
                session_key=key,
                record_id=intent.id,
                goal_id=intent.goal_id,
                role="decision",
                model_id=intent.brain_model_id,
                engine=snapshot.engine,
                native_session_id=intent.native_session_id,
                permission_mode=intent.brain_permission_mode,
                operation_status=intent.status,
                runtime=snapshot.runtime,
            )
        )
    activity_gaps, activity_limited = _native_activity(project, nodes, references, events)
    gaps += activity_gaps
    primary_sessions = tuple(row.session_key for row in nodes if row.role != "external")
    limited = (
        len(rows) > ROUND_LIMIT
        or len(events) > MAX_EVENTS
        or len(nodes) >= TASK_LIMIT
        or activity_limited
    )
    flow = project_flow(
        str(project),
        nodes,
        sorted(events, key=lambda item: item.seq)[-MAX_EVENTS:],
        focus_sessions=primary_sessions[:100],
    )
    visible = {node.session_key for node in flow.nodes}
    return HostTrace(
        flow=flow,
        references=tuple(ref for ref in references if ref.session_key in visible),
        observation_gaps=gaps,
        window_limited=limited,
    )


def read_host_trace(project: Path, *, probe: ProcessProbe | None = None) -> HostTrace:
    """Open only the selected existing project database in SQLite read-only mode."""
    root = project.resolve(strict=True)
    path = owned_path(root, Path(".model-desk/records.db"))
    if not path.is_file():
        return HostTrace(
            flow=project_flow(str(root), (), ()),
            references=(),
            observation_gaps=0,
            window_limited=False,
        )
    with closing(connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as database:
        _ = database.execute("BEGIN")
        return _read(root, database, probe)
