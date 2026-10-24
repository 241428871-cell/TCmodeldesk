"""Native event projections summarize tools and subagents without copying file bodies."""

import json

from pydantic import JsonValue

from model_desk_control.native_events import parse_events


def lines(*events: dict[str, JsonValue]) -> str:
    return "\n".join(json.dumps(event) for event in events)


def test_claude_stream_tools_results_and_subagents() -> None:
    hidden_body = "FILE-BODY-SHOULD-NOT-APPEAR"
    text = lines(
        {"type": "system", "subtype": "init", "model": "claude-x", "permissionMode": "auto"},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Checking inputs"},
                    {
                        "type": "tool_use",
                        "id": "toolu_task",
                        "name": "Write",
                        "input": {"file_path": "notes.md", "content": hidden_body},
                    },
                ]
            },
        },
        {
            "type": "user",
            "parent_tool_use_id": "toolu_task",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_task",
                        "is_error": True,
                        "content": "denied",
                    }
                ]
            },
        },
        {"type": "result", "subtype": "success", "is_error": False},
    )
    view = parse_events(text, partial_first_line=False)
    assert view.format == "claude-stream"
    kinds = [event.kind for event in view.events]
    assert kinds == ["session", "message", "tool", "tool_result", "result"]
    assert view.events[2].detail == "file_path=notes.md"
    assert view.events[2].ref == "toolu_task"
    assert hidden_body not in view.model_dump_json()
    assert view.events[3].parent == "toolu_task"
    assert view.events[3].status == "error"


def test_codex_items_and_partial_leading_line() -> None:
    text = '{"type":"item.comp\n' + lines(
        {"type": "thread.started", "thread_id": "thread-1"},
        {
            "type": "item.completed",
            "item": {"type": "command_execution", "command": "pytest -q", "exit_code": 0},
        },
        {
            "type": "item.completed",
            "item": {"type": "file_change", "changes": [{"path": "a.py", "kind": "update"}]},
        },
        {"type": "turn.failed", "error": {"message": "quota"}},
    )
    view = parse_events(text, partial_first_line=True)
    assert view.format == "codex-json"
    assert [event.kind for event in view.events] == ["session", "command", "file_change", "error"]
    assert view.events[1].detail == "exit_code=0"
    assert view.events[2].detail == "update a.py"
    assert view.truncated
    assert view.skipped_lines == 0


def test_plain_text_is_unknown_not_guessed() -> None:
    view = parse_events("plain output\nmore", partial_first_line=False)
    assert view.format == "unknown"
    assert view.events == ()
    assert view.skipped_lines == 2
