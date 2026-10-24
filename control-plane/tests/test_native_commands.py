"""Native permission snapshots map to exact argv without starting either CLI."""

from dataclasses import replace
from pathlib import Path
from typing import assert_never

import pytest

from model_desk_control.native_commands import (
    RECORD_EDIT_DENY,
    NativeCommandSpec,
    NativeHost,
    NativeMode,
    build_native_command,
)


def spec_at(directory: Path, host: NativeHost, mode: NativeMode) -> NativeCommandSpec:
    return NativeCommandSpec(
        host=host,
        mode=mode,
        executable="/fixture/native-cli",
        model="exact-account-model",
        project=directory / "project",
        output=directory / "result.json",
        schema=directory / "schema.json",
        schema_json='{"type":"object"}',
        runtime_args=("--effort", "high")
        if host is NativeHost.CLAUDE
        else ("-c", 'model_reasoning_effort="high"'),
    )


@pytest.mark.parametrize("host", [NativeHost.CLAUDE, NativeHost.CODEX])
@pytest.mark.parametrize("session", ["", "original-session"])
def test_auto_uses_native_automatic_review_for_new_and_resumed_session(
    tmp_path: Path, host: NativeHost, session: str
) -> None:
    spec = spec_at(tmp_path, host, NativeMode.AUTO)
    launch = build_native_command(replace(spec, session_id=session))
    arguments = launch.arguments
    assert "--dangerously-bypass-approvals-and-sandbox" not in arguments
    assert "--dangerously-skip-permissions" not in arguments
    assert "bypassPermissions" not in arguments
    match host:
        case NativeHost.CLAUDE:
            assert arguments[arguments.index("--output-format") + 1] == "stream-json"
            assert "--verbose" in arguments
            assert "--forward-subagent-text" in arguments
            assert arguments[arguments.index("--permission-mode") + 1] == "auto"
            assert arguments[arguments.index("--tools") + 1] == "default"
            assert arguments[arguments.index("--disallowedTools") + 1] == RECORD_EDIT_DENY
            assert "--restricted" not in arguments
            if session:
                assert arguments[-2:] == ("--resume", session)
        case NativeHost.CODEX:
            assert 'approval_policy="on-request"' in arguments
            assert 'approvals_reviewer="auto_review"' in arguments
            assert 'sandbox_mode="workspace-write"' in arguments
            assert "features.skills=false" not in arguments
            if session:
                assert arguments[1:3] == ("exec", "resume")
                assert arguments[-2:] == (session, "-")
        case _:
            assert_never(host)
    if session:
        assert launch.session_id == session


@pytest.mark.parametrize("host", [NativeHost.CLAUDE, NativeHost.CODEX])
def test_explicit_historical_read_only_mode_remains_read_only(
    tmp_path: Path, host: NativeHost
) -> None:
    arguments = build_native_command(spec_at(tmp_path, host, NativeMode.READ_ONLY)).arguments
    match host:
        case NativeHost.CLAUDE:
            assert arguments[arguments.index("--permission-mode") + 1] == "plan"
            assert arguments[arguments.index("--tools") + 1] == "Read,Glob,Grep"
        case NativeHost.CODEX:
            assert 'sandbox_mode="read-only"' in arguments
            assert 'approval_policy="never"' in arguments
        case _:
            assert_never(host)


def test_command_construction_preserves_model_and_profile_without_creating_files(
    tmp_path: Path,
) -> None:
    spec = spec_at(tmp_path, NativeHost.CODEX, NativeMode.AUTO)
    arguments = build_native_command(spec).arguments
    assert arguments[arguments.index("--model") + 1] == "exact-account-model"
    assert 'model_reasoning_effort="high"' in arguments
    assert arguments[arguments.index("--cd") + 1] == str(spec.project)
    assert list(tmp_path.iterdir()) == []
