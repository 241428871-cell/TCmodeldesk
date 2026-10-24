"""Pure state-machine and dynamic-plan contracts have no provider effects."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable
from pydantic import ValidationError

from model_desk_control.control import (
    ContractError,
    Control,
    UserIntent,
    backoff,
    claim_tick,
    close,
    pause,
    resume,
    set_resource_hold,
    stop,
)
from model_desk_control.plans import Plan, PlanPatch, TaskSpec, apply_patch


def initial() -> Control:
    return Control(goal_id="project-a/goal-a", due_at=100)


def task(task_id: str, dependencies: tuple[str, ...] = ()) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        objective=f"Deliver {task_id}",
        acceptance=("Evidence exists",),
        dependencies=dependencies,
    )


def test_duplicate_tick_is_rejected_by_revision_and_busy() -> None:
    state = claim_tick(initial(), epoch=0, revision=0, now=100)
    assert state.busy
    assert state.due_at == 1900
    with pytest.raises(ContractError, match="stale_authority_or_revision"):
        _ = claim_tick(state, epoch=0, revision=0, now=100)
    with pytest.raises(ContractError, match="not_eligible"):
        _ = claim_tick(state, epoch=0, revision=1, now=2000)


def test_stop_revokes_stale_events_without_claiming_worker_exit() -> None:
    running = claim_tick(initial(), epoch=0, revision=0, now=100)
    stopped = stop(running)
    assert stopped.epoch == 1
    assert stopped.busy
    assert stopped.due_at is None
    with pytest.raises(ContractError, match="stale_authority_or_revision"):
        _ = claim_tick(stopped, epoch=0, revision=1, now=10000)
    with pytest.raises(ContractError, match="resume_not_authorized_or_worker_active"):
        _ = resume(stopped, now=10000, explicit_user_command=True)


@pytest.mark.parametrize("user_control", [pause, stop])
def test_resource_recovery_does_not_restore_user_authority(
    user_control: Callable[[Control], Control],
) -> None:
    state = user_control(initial())
    recovered = set_resource_hold(state, held=False)
    assert recovered.intent == state.intent
    assert recovered.epoch == state.epoch
    assert recovered.due_at == state.due_at
    with pytest.raises(ContractError, match="not_eligible"):
        _ = claim_tick(recovered, epoch=recovered.epoch, revision=recovered.revision, now=2000)


def test_resume_requires_new_user_command_and_never_advances_existing_due_time() -> None:
    state = pause(initial())
    with pytest.raises(ContractError, match="resume_not_authorized_or_worker_active"):
        _ = resume(state, now=10, explicit_user_command=False)
    resumed = resume(state, now=10, explicit_user_command=True)
    assert resumed.epoch == 2
    assert resumed.due_at == 100


def test_resource_hold_is_independent_and_cannot_run_early() -> None:
    held = set_resource_hold(initial(), held=True)
    assert held.intent is UserIntent.RUN
    with pytest.raises(ContractError, match="not_eligible"):
        _ = claim_tick(held, epoch=0, revision=1, now=100)
    recovered = set_resource_hold(held, held=False)
    with pytest.raises(ContractError, match="not_eligible"):
        _ = claim_tick(recovered, epoch=0, revision=2, now=99)


def test_backoff_cannot_shorten_or_rearm_stopped_clock() -> None:
    with pytest.raises(ContractError, match="interval_cannot_shorten"):
        _ = backoff(initial(), interval=1799, now=0)
    assert backoff(initial(), interval=3600, now=100).due_at == 3700
    assert backoff(stop(initial()), interval=3600, now=100).due_at is None


def test_implementation_and_save_do_not_skip_acceptance_or_cleanup() -> None:
    state = initial().model_copy(update={"goal_complete": True, "saved": True})
    with pytest.raises(ContractError, match="closure_obligations_incomplete"):
        _ = close(state)
    complete = state.model_copy(update={"analyzed": True, "accepted": True, "cleaned": True})
    closed = close(complete)
    assert closed.closed
    assert closed.due_at is None
    with pytest.raises(ContractError, match="closed"):
        _ = resume(closed, now=200, explicit_user_command=True)


def test_invalid_persisted_clock_cannot_load() -> None:
    with pytest.raises(ValidationError, match="running_requires_clock"):
        _ = Control(goal_id="goal", due_at=None)
    with pytest.raises(ValidationError):
        _ = Control(goal_id="goal", due_at=0, interval=0)


def test_ordinary_plan_can_change_without_human_signoff() -> None:
    revised = apply_patch(
        Plan(),
        PlanPatch(
            base_revision=0,
            reason="investigated",
            added=(task("inspect"), task("fix", ("inspect",))),
        ),
        active_task_ids=frozenset(),
    )
    assert revised.revision == 1
    assert [item.task_id for item in revised.tasks] == ["inspect", "fix"]
    replaced = apply_patch(
        revised,
        PlanPatch(
            base_revision=1,
            reason="better route",
            superseded=("fix",),
            added=(task("alternative"),),
        ),
        active_task_ids=frozenset(),
    )
    assert replaced.superseded == ("fix",)
    assert len(replaced.tasks) == 3


@pytest.mark.parametrize(
    ("specs", "reason"),
    [
        ((task("a", ("b",)), task("b", ("a",))), "dependency_cycle"),
        ((task("a", ("absent",)),), "unknown_or_superseded_dependency"),
        ((task("a"), task("a")), "duplicate_task_identity"),
    ],
)
def test_invalid_dynamic_graph_is_rejected(specs: tuple[TaskSpec, ...], reason: str) -> None:
    with pytest.raises(ContractError, match=reason):
        _ = apply_patch(
            Plan(),
            PlanPatch(base_revision=0, reason="test", added=specs),
            active_task_ids=frozenset(),
        )


def test_active_replacement_requires_separate_cancellation_and_stale_patch_rejected() -> None:
    plan = Plan(revision=3, tasks=(task("running"),))
    with pytest.raises(ContractError, match="cannot_replace_unknown_or_active_task"):
        _ = apply_patch(
            plan,
            PlanPatch(base_revision=3, reason="new", superseded=("running",)),
            active_task_ids=frozenset({"running"}),
        )
    with pytest.raises(ContractError, match="plan_revision_conflict"):
        _ = apply_patch(
            plan, PlanPatch(base_revision=2, reason="stale"), active_task_ids=frozenset()
        )
