"""Acceptance, stage observation and deletion cannot outrun resource release."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Protocol

import pytest
from pydantic import JsonValue

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import delegation_policy
import execution_leases
import lease_guard
import lease_runtime
import model_registry as registry
import stage_store
import task_history
from test_execution_leases import MODEL, LeaseFixture
from test_execution_leases import fixture as fixture

type Record = dict[str, JsonValue]


class Policy(Protocol):
    def record_executor(self, run: Path, executor: Record) -> Record | None: ...
    def accept(self, run: Path, actor: str, criteria: list[Record]) -> Record: ...


def policy_adapter() -> Policy:
    return delegation_policy


def prepared(fixture: LeaseFixture) -> tuple[Path, str]:
    run = fixture.task("gated")
    state = registry.patch_task(
        run,
        status="completed",
        delegation={"schema": 1},
        route={"requirements": {"acceptance": "A"}},
    )
    receipt = lease_runtime.reserve(execution_leases.request_for(state, MODEL))
    assert receipt is not None
    _ = registry.patch_task(run, execution_lease_token=receipt.token, execution_lease_protocol=1)
    policy = policy_adapter()
    _ = policy.record_executor(run, {"completed": True, "test": {"exit_code": 0}})
    return run, receipt.token


@pytest.mark.parametrize("held", [True, False])
def test_independent_acceptance_requires_released_resources(
    fixture: LeaseFixture, *, held: bool
) -> None:
    # Given valid independent evidence but optionally still-held execution resources.
    run, token = prepared(fixture)
    if not held:
        assert lease_runtime.release(token)
    policy = policy_adapter()
    evidence: list[Record] = [
        {
            "id": "c1",
            "status": "pass",
            "evidence": [{"source": "reviewer_test", "detail": "isolated check", "exit_code": 0}],
        }
    ]
    before = (run / "review.json").read_bytes()
    # When independent acceptance is submitted through its existing entrypoint.
    if held:
        with pytest.raises(
            ValueError, match=re.escape(lease_guard.reason(registry.read_task(run)))
        ) as error:
            _ = policy.accept(run, "main", evidence)
        # Then a held resource blocks the decision without changing its record.
        assert str(error.value) == lease_guard.reason(registry.read_task(run))
        assert (run / "review.json").read_bytes() == before
    else:
        result = policy.accept(run, "main", evidence)
        assert result["state"] == "accepted"


def test_completed_report_keeps_stage_running_until_release(fixture: LeaseFixture) -> None:
    # Given a completion report while a resource reservation is still active.
    run, _ = prepared(fixture)
    # When the normal stage observer reads the saved result.
    result = stage_store.sync_stage(
        {"status": "dispatched"}, registry.read_task(run), {"state": "executor_completed"}
    )
    # Then it cannot make the stage eligible for acceptance or dependent dispatch.
    assert result == "running"


def test_terminal_task_cannot_be_deleted_while_lease_is_held(fixture: LeaseFixture) -> None:
    # Given a terminal task label with a durable resource hold.
    run, _ = prepared(fixture)
    # When deletion is requested through the real preview API.
    with pytest.raises(ValueError, match=re.escape(lease_guard.reason(registry.read_task(run)))):
        _ = task_history.preview({"mode": "project", "project": str(fixture.project)}, [run.name])
    # Then the complete original task remains available for recovery.
    assert (run / "task.json").is_file()


def test_protocol_marker_prevents_missing_token_legacy_fallback(fixture: LeaseFixture) -> None:
    # Given a protocol task whose token has been lost.
    run = fixture.task("missing")
    state = registry.patch_task(run, status="completed", execution_lease_protocol=1)
    # When acceptance inspects the task.
    result = lease_guard.reason(state)
    # Then it cannot be interpreted as an unleased historical task.
    assert result


def test_cancelled_label_does_not_hide_goal_resources(fixture: LeaseFixture) -> None:
    # Given a cancelled task still holding resources for its original goal.
    run, _ = prepared(fixture)
    _ = registry.patch_task(run, status="cancelled", goal_id="goal_original")
    # When an owner handoff checks that goal's execution resources.
    reason = lease_guard.goal_reason(str(fixture.project), "goal_original")
    # Then unrelated goals remain independent, while this goal cannot transfer yet.
    assert reason
    assert lease_guard.goal_reason(str(fixture.project), "goal_other") == ""
