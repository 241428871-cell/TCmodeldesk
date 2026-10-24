"""Structured, read-only views of native JSON event streams; raw output stays authoritative."""

from typing import Final, Literal

from pydantic import Field, JsonValue, TypeAdapter, ValidationError

from model_desk_control.trace_models import FlowRecord

MAX_EVENTS: Final = 200
TITLE: Final = 200
DETAIL: Final = 500
# Only short locating parameters are shown; file bodies and edit contents never are.
TOOL_KEYS: Final = (
    "file_path",
    "path",
    "command",
    "pattern",
    "url",
    "description",
    "subagent_type",
)
_LINE: Final = TypeAdapter(dict[str, JsonValue])

type EventKind = Literal[
    "session",
    "message",
    "tool",
    "tool_result",
    "command",
    "file_change",
    "result",
    "error",
]
type StreamFormat = Literal["claude-stream", "codex-json", "unknown"]


class NativeEvent(FlowRecord):
    """One summarized event; parent links a subagent's events to the spawning tool call."""

    kind: EventKind
    title: str = Field(max_length=TITLE)
    detail: str = Field(default="", max_length=DETAIL)
    ref: str = Field(default="", max_length=100)
    parent: str = Field(default="", max_length=100)
    status: str = Field(default="", max_length=40)


class NativeEvents(FlowRecord):
    """A bounded projection of the tail; unparsable lines are counted, not guessed."""

    format: StreamFormat
    events: tuple[NativeEvent, ...]
    skipped_lines: int
    truncated: bool


def _text(value: JsonValue, limit: int) -> str:
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, bool | int | float):
        return str(value)[:limit]
    return ""


def _map(value: JsonValue) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def _items(value: JsonValue) -> list[JsonValue]:
    return value if isinstance(value, list) else []


def _tool_detail(raw: JsonValue) -> str:
    source = _map(raw)
    parts = [key + "=" + _text(source[key], 160) for key in TOOL_KEYS if _text(source.get(key), 1)]
    return "; ".join(parts)[:DETAIL]


def _result_text(raw: JsonValue) -> str:
    if isinstance(raw, str):
        return raw[:DETAIL]
    for block in _items(raw):
        text = _text(_map(block).get("text"), DETAIL)
        if text:
            return text
    return ""


def _claude_blocks(event: dict[str, JsonValue], parent: str) -> list[NativeEvent]:
    found: list[NativeEvent] = []
    for raw in _items(_map(event.get("message")).get("content")):
        block = _map(raw)
        match _text(block.get("type"), 40):
            case "text":
                text = _text(block.get("text"), DETAIL)
                if text.strip():
                    found.append(
                        NativeEvent(kind="message", title="回复", detail=text, parent=parent)
                    )
            case "tool_use":
                found.append(
                    NativeEvent(
                        kind="tool",
                        title=_text(block.get("name"), TITLE) or "工具",
                        detail=_tool_detail(block.get("input")),
                        ref=_text(block.get("id"), 100),
                        parent=parent,
                    )
                )
            case "tool_result":
                failed = block.get("is_error") is True
                found.append(
                    NativeEvent(
                        kind="tool_result",
                        title="工具结果",
                        detail=_result_text(block.get("content")),
                        parent=_text(block.get("tool_use_id"), 100) or parent,
                        status="error" if failed else "ok",
                    )
                )
            case _:
                pass
    return found


def _claude(event: dict[str, JsonValue]) -> list[NativeEvent]:
    parent = _text(event.get("parent_tool_use_id"), 100)
    match _text(event.get("type"), 40):
        case "system" if event.get("subtype") == "init":
            detail = " ".join(
                part
                for part in (
                    _text(event.get("model"), 100),
                    _text(event.get("permissionMode"), 40),
                )
                if part
            )
            return [NativeEvent(kind="session", title="会话开始", detail=detail, parent=parent)]
        case "assistant" | "user":
            return _claude_blocks(event, parent)
        case "result":
            failed = event.get("is_error") is True
            return [
                NativeEvent(
                    kind="error" if failed else "result",
                    title="本轮结束",
                    detail=_text(event.get("subtype"), 40),
                    parent=parent,
                    status="error" if failed else "ok",
                )
            ]
        case _:
            return []


def _codex_item(item: dict[str, JsonValue], phase: str) -> list[NativeEvent]:
    status = _text(item.get("status"), 40) or phase
    ref = _text(item.get("id"), 100)
    match _text(item.get("type"), 40):
        case "agent_message":
            return [
                NativeEvent(kind="message", title="回复", detail=_text(item.get("text"), DETAIL))
            ]
        case "command_execution":
            code = _text(item.get("exit_code"), 20)
            return [
                NativeEvent(
                    kind="command",
                    title=_text(item.get("command"), TITLE) or "命令",
                    detail="exit_code=" + code if code else "",
                    ref=ref,
                    status=status,
                )
            ]
        case "file_change":
            paths = [
                _text(_map(change).get("kind"), 20) + " " + _text(_map(change).get("path"), 200)
                for change in _items(item.get("changes"))
            ]
            return [
                NativeEvent(
                    kind="file_change",
                    title="文件变更",
                    detail="; ".join(paths)[:DETAIL],
                    ref=ref,
                    status=status,
                )
            ]
        case "mcp_tool_call":
            title = _text(item.get("server"), 80) + "/" + _text(item.get("tool"), 100)
            return [NativeEvent(kind="tool", title=title[:TITLE], ref=ref, status=status)]
        case "error":
            return [
                NativeEvent(kind="error", title="错误", detail=_text(item.get("message"), DETAIL))
            ]
        case _:
            return []


def _codex(event: dict[str, JsonValue]) -> list[NativeEvent]:
    kind = _text(event.get("type"), 40)
    match kind:
        case "thread.started":
            detail = _text(event.get("thread_id"), 100)
            return [NativeEvent(kind="session", title="会话开始", detail=detail)]
        case "item.started" | "item.completed":
            return _codex_item(_map(event.get("item")), kind.removeprefix("item."))
        case "turn.completed":
            return [NativeEvent(kind="result", title="本轮结束", status="ok")]
        case "turn.failed" | "error":
            message = _text(_map(event.get("error")).get("message") or event.get("message"), DETAIL)
            return [NativeEvent(kind="error", title="本轮失败", detail=message, status="error")]
        case _:
            return []


def _format(event: dict[str, JsonValue]) -> StreamFormat:
    kind = _text(event.get("type"), 40)
    if kind in {"system", "assistant", "user", "result"}:
        return "claude-stream"
    if kind.startswith(("thread.", "turn.", "item.")):
        return "codex-json"
    return "unknown"


def parse_events(text: str, *, partial_first_line: bool) -> NativeEvents:
    """Project a JSONL tail; a truncated leading line is skipped instead of being repaired."""
    lines = text.splitlines()
    if partial_first_line and lines:
        lines = lines[1:]
    stream: StreamFormat = "unknown"
    events: list[NativeEvent] = []
    skipped = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            event = _LINE.validate_json(line)
        except ValidationError:
            skipped += 1
            continue
        if stream == "unknown":
            stream = _format(event)
        match stream:
            case "claude-stream":
                events.extend(_claude(event))
            case "codex-json":
                events.extend(_codex(event))
            case "unknown":
                skipped += 1
    return NativeEvents(
        format=stream,
        events=tuple(events[-MAX_EVENTS:]),
        skipped_lines=skipped,
        truncated=len(events) > MAX_EVENTS or partial_first_line,
    )
