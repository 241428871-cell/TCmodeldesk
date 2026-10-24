"""Read-only panel adapter for scoped domain flow and captured native output."""

from pathlib import Path
from typing import Literal, assert_never
from urllib.parse import parse_qs, unquote, urlsplit

import control_bootstrap


class FlowQueryError(ValueError):
    """A malformed read query never reaches any native output source."""

    def __init__(self, code: str) -> None:
        """Retain only a safe stable parser reason."""
        self.code: str = code
        super().__init__(code)


def _channel(query: str) -> Literal["stdout", "stderr"]:
    channels = parse_qs(query, keep_blank_values=True).get("channel", ["stdout"])
    if channels not in (["stdout"], ["stderr"]):
        code = "invalid_output_channel"
        raise FlowQueryError(code)
    return "stderr" if channels == ["stderr"] else "stdout"


def _structured(query: str) -> bool:
    views = parse_qs(query, keep_blank_values=True).get("view", ["raw"])
    if views not in (["raw"], ["events"]):
        code = "invalid_output_view"
        raise FlowQueryError(code)
    return views == ["events"]


def _view(project: Path, relative: Path, channel: Literal["stdout", "stderr"], query: str) -> str:
    """Raw tail by default; the events view projects the same bounded tail."""
    from model_desk_control.native_events import parse_events
    from model_desk_control.native_output import output_tail

    tail = output_tail(project, relative, channel)
    if not _structured(query):
        return tail.model_dump_json()
    return parse_events(tail.text, partial_first_line=tail.omitted_bytes > 0).model_dump_json()


def get(request_path: str, project: str | None) -> str | None:
    """Return serialized view data; this adapter never starts a CLI or initializes a store."""
    path = urlsplit(request_path).path
    if not (path == "/api/flow" or path.startswith("/api/flow/sessions/")):
        return None
    if not project:
        message = "请先选择具体项目后查看消息流和原生输出"
        raise PermissionError(message)
    control_bootstrap.bind_source()
    import goal_brain_runner

    from model_desk_control.host_trace import read_host_trace
    from model_desk_control.native_output import NativeReadError

    trace = read_host_trace(
        Path(project), probe=lambda pid, birth: goal_brain_runner.inspect_owned(pid, birth)[0]
    )
    if path == "/api/flow":
        return trace.model_dump_json()
    prefix = "/api/flow/sessions/"
    key = unquote(path[len(prefix) :])
    ref = next((item for item in trace.references if item.session_key == key), None)
    if ref is None:
        code = "session_not_in_selected_project"
        raise NativeReadError(code)
    query = parse_qs(urlsplit(request_path).query, keep_blank_values=True)
    channel = _channel(urlsplit(request_path).query)
    match ref.role:
        case "decision":
            name = "native-errors.txt" if channel == "stderr" else "native-output.jsonl"
            relative = Path(".model-desk/goal-brain") / ref.goal_id / ref.record_id / name
            return _view(Path(project), relative, channel, urlsplit(request_path).query)
        case "execution":
            if query.get("capture_id", [""]) != [ref.native_capture_id]:
                code = "native_capture_changed_select_session_again"
                raise NativeReadError(code)
            node = next(item for item in trace.flow.nodes if item.session_key == key)
            if node.view_kind != "native_events":
                code = "execution_output_adapter_unavailable"
                raise NativeReadError(code)
            name = "native-errors.txt" if channel == "stderr" else "native-output.txt"
            relative = Path(".model-desk/runs") / ref.record_id / name
            if ref.native_capture_id:
                relative = relative.parent / "native-cli" / ref.native_capture_id / name
            return _view(Path(project), relative, channel, urlsplit(request_path).query)
        case _:
            assert_never(ref.role)
