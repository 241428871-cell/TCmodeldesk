"""Domain rollback and late settlement preserve the one-current-continuation invariant."""

from pathlib import Path
from sqlite3 import Connection

import pytest

from model_desk_control import round_finalize
from model_desk_control.authority import read_control, write_control
from model_desk_control.control import resume
from model_desk_control.outbox import OutboxStore
from model_desk_control.plan_store import PlanStore
from model_desk_control.round_models import PlanningRound, RoundDisposition, RoundStage
from tests.round_fixtures import feedback_for, prepared_round


class SimulatedDomainWriteLossError(RuntimeError):
    """A fault after plan SQL writes but before round settlement."""


def test_plan_round_and_busy_updates_rollback_together_on_domain_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controls, lease, rounds = prepared_round(tmp_path)
    _, record = rounds.choose(lease, now=101)
    _ = rounds.observe(lease.goal_id, feedback_for(record.request))

    def fail_round_write(_database: Connection, _record: PlanningRound) -> None:
        reason = "simulated write failure after plan update"
        raise SimulatedDomainWriteLossError(reason)

    with monkeypatch.context() as fault:
        fault.setattr(round_finalize, "write_round", fail_round_write)
        with pytest.raises(SimulatedDomainWriteLossError):
            _ = rounds.finalize(lease)
    assert PlanStore(controls).read(lease.goal_id).revision == 1
    assert controls.read(lease.goal_id).busy
    assert rounds.read(lease.goal_id, lease.operation_id).stage is RoundStage.RESULT
    assert rounds.finalize(lease).disposition is RoundDisposition.APPLIED


def test_replayed_old_settlement_does_not_clear_new_epoch_active_work(tmp_path: Path) -> None:
    controls, lease, rounds = prepared_round(tmp_path)
    _, record = rounds.choose(lease, now=101)
    _ = controls.stop(goal_id=lease.goal_id, request_id="stop")
    _ = rounds.observe(lease.goal_id, feedback_for(record.request))
    original_receipt = rounds.finalize(lease)
    # Trusted fixture applies an explicit user resume; production control API is not wired yet.
    with controls.connection() as database, database:
        prior = read_control(database, lease.goal_id)
        changed = resume(prior, now=200, explicit_user_command=True)
        write_control(database, prior, changed)
    active = controls.claim(
        goal_id=lease.goal_id,
        request_id="new-epoch-tick",
        epoch=changed.epoch,
        revision=changed.revision,
        now=200,
    )
    assert (
        OutboxStore(controls).take(
            goal_id=lease.goal_id, owner="new-owner", now=200, lease_seconds=30
        )
        is not None
    )
    assert rounds.finalize(lease) == original_receipt
    assert controls.read(lease.goal_id) == active
    assert active.busy
