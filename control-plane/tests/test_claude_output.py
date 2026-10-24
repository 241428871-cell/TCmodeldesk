"""Streamed text and tools remain observations until the main result proves completion."""

import json

import pytest
from pydantic import JsonValue

from model_desk_control.claude_output import (
    ClaudeOutputError,
    completion_from_jsonl,
    session_from_jsonl,
)


def terminal(**changes: JsonValue) -> str:
    value: dict[str, JsonValue] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "session_id": "main-session",
        "result": "Delivered result",
        **changes,
    }
    return json.dumps(value)


def test_message_blocks_are_not_mistaken_for_final_delivery() -> None:
    events = (
        '{"type":"system","subtype":"init","session_id":"main-session"}',
        '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Read"}]}}',
        '{"type":"user","parent_tool_use_id":"agent-call","message":{"content":"child"}}',
        terminal(),
    )
    stream = "\n".join(events)
    completion = completion_from_jsonl(stream, expected_session="main-session")
    assert completion.result == "Delivered result"
    assert completion.session_id == "main-session"


def test_structured_actions_survive_stream_parsing() -> None:
    value: dict[str, JsonValue] = {"plan": None, "dispatch_stage_ids": ["build"], "complete": False}
    completion = completion_from_jsonl(terminal(structured_output=value))
    assert completion.structured_output == value


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("", "claude_result_missing"),
        ('{"type":"assistant","message":{"content":"unfinished"}}', "claude_result_missing"),
        ('{"type":"stream_event","event":{"type":"message_stop"}}', "claude_result_missing"),
        ("not json", "claude_stream_invalid"),
        ("[]", "claude_stream_invalid"),
        ('{"result":"a terminal without proof"}', "claude_stream_invalid"),
        (terminal(is_error="false"), "claude_stream_invalid"),
        (terminal(session_id=""), "claude_stream_invalid"),
        (terminal(is_error=True), "claude_result_failed"),
        (terminal(subtype="error_max_turns"), "claude_result_failed"),
        (terminal(parent_tool_use_id="child"), "claude_result_failed"),
        (terminal() + "\n" + terminal(), "claude_stream_continues_after_result"),
        (terminal() + '\n{"type":"assistant"}', "claude_stream_continues_after_result"),
    ],
)
def test_invalid_or_incomplete_native_stream_never_becomes_success(raw: str, code: str) -> None:
    with pytest.raises(ClaudeOutputError) as error:
        _ = completion_from_jsonl(raw)
    assert error.value.code == code


def test_terminal_result_cannot_rebind_the_existing_native_session() -> None:
    with pytest.raises(ClaudeOutputError, match="claude_session_mismatch"):
        _ = completion_from_jsonl(terminal(), expected_session="another-session")


def test_blank_lines_after_terminal_do_not_create_false_additional_events() -> None:
    assert completion_from_jsonl(terminal() + "\n \n").result == "Delivered result"


def test_stream_reading_has_a_byte_budget_before_json_parsing() -> None:
    with pytest.raises(ClaudeOutputError, match="claude_stream_size_exceeded"):
        _ = completion_from_jsonl("界" * 1_400_000)


def test_effective_session_comes_from_the_main_init_event() -> None:
    child: dict[str, JsonValue] = {
        "type": "system",
        "subtype": "init",
        "parent_tool_use_id": "child",
        "model": "other",
        "permissionMode": "bypassPermissions",
    }
    main: dict[str, JsonValue] = {
        "type": "system",
        "subtype": "init",
        "model": "claude-opus",
        "permissionMode": "auto",
        "claude_code_version": "2.1.309",
    }
    session = session_from_jsonl("\n".join([json.dumps(child), "not json", json.dumps(main)]))
    assert session is not None
    assert (session.model, session.permission_mode, session.version) == (
        "claude-opus",
        "auto",
        "2.1.309",
    )
    assert session_from_jsonl('{"type":"result"}') is None
