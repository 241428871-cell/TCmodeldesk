"""Declaration boundaries must not promote readiness or partial guards to isolation."""

import sys

import pytest
from pydantic import ValidationError

from model_desk_control.enforcement import EnforcementReport, EnforcementScope, report


@pytest.mark.parametrize("engine", ["claude_cli", "dsh", "pi", "workbuddy"])
def test_tools_and_permissions_do_not_claim_native_sandbox(engine: str) -> None:
    declared = report(engine, "execution")
    if engine == "claude_cli" and sys.platform == "darwin":
        assert declared.enforcement_scope is EnforcementScope.SANDBOX
    else:
        assert declared.enforcement_scope is not EnforcementScope.SANDBOX
    assert declared.runtime_verified is False
    assert declared.limitations


@pytest.mark.parametrize("engine", ["dsh", "pi", "workbuddy", "future_host", ""])
def test_execution_support_does_not_invent_native_brain_support(engine: str) -> None:
    declared = report(engine, "decision")
    assert declared.enforcement_scope is EnforcementScope.UNSUPPORTED
    assert declared.controls == ()


def test_claude_worker_does_not_inherit_brain_tool_denial() -> None:
    worker = report("claude_cli", "execution")
    brain = report("claude_cli", "decision")
    if sys.platform == "darwin":
        assert worker.enforcement_scope is EnforcementScope.SANDBOX
        assert brain.enforcement_scope is EnforcementScope.SANDBOX
    else:
        assert worker.enforcement_scope is EnforcementScope.DISPATCH
        assert brain.enforcement_scope is EnforcementScope.TOOLS
    assert all("Edit(" not in control for control in worker.controls)


@pytest.mark.parametrize("engine", ["claude_cli", "codex_cli"])
def test_native_brain_declares_disposable_project_boundary(engine: str) -> None:
    brain = report(engine, "decision")
    assert any("隔离副本" in control for control in brain.controls)
    assert any("结构化动作" in control for control in brain.controls)
    assert brain.runtime_verified is False


def test_runtime_proof_cannot_be_fabricated_in_a_declaration() -> None:
    declared = report("codex_cli", "decision")
    with pytest.raises(ValidationError, match="runtime_verified"):
        _ = EnforcementReport.model_validate({**declared.model_dump(), "runtime_verified": True})


def test_unknown_role_has_no_scope_even_for_a_known_engine() -> None:
    assert report("codex_cli", "typo").enforcement_scope is EnforcementScope.UNSUPPORTED
