"""Only fresh authority can consume old commands; expiry never means re-send."""

from pathlib import Path

from model_desk_control.control import Control, UserIntent
from model_desk_control.outbox import OutboxStore
from model_desk_control.store import ControlStore


def prepared(directory: Path) -> ControlStore:
    controls = ControlStore(directory / "authority.sqlite")
    controls.initialize()
    controls.create(Control(goal_id="p/g", due_at=100))
    _ = controls.claim(goal_id="p/g", request_id="tick", epoch=0, revision=0, now=100)
    return controls


def test_only_one_consumer_gets_pending_intent(tmp_path: Path) -> None:
    controls = prepared(tmp_path)
    outbox = OutboxStore(controls)
    lease = outbox.take(goal_id="p/g", owner="worker-a", now=100, lease_seconds=30)
    assert lease is not None
    assert outbox.take(goal_id="p/g", owner="worker-b", now=100, lease_seconds=30) is None
    assert outbox.authorize(lease, now=101)


def test_stop_cancels_unsent_work_even_if_old_claim_receipt_is_replayed(tmp_path: Path) -> None:
    controls = prepared(tmp_path)
    _ = controls.stop(goal_id="p/g", request_id="stop")
    historical_receipt = controls.claim(
        goal_id="p/g", request_id="tick", epoch=0, revision=0, now=100
    )
    assert historical_receipt.intent is UserIntent.RUN
    assert controls.read("p/g").intent is UserIntent.STOP
    assert (
        OutboxStore(controls).take(goal_id="p/g", owner="worker", now=101, lease_seconds=30) is None
    )
    assert controls.pending_count("p/g") == 0


def test_stop_revokes_taken_lease_before_effect(tmp_path: Path) -> None:
    controls = prepared(tmp_path)
    outbox = OutboxStore(controls)
    lease = outbox.take(goal_id="p/g", owner="worker", now=100, lease_seconds=30)
    assert lease is not None
    _ = controls.stop(goal_id="p/g", request_id="stop")
    assert not outbox.authorize(lease, now=101)


def test_expired_lease_is_unknown_not_an_automatically_retriable_prompt(tmp_path: Path) -> None:
    controls = prepared(tmp_path)
    outbox = OutboxStore(controls)
    lease = outbox.take(goal_id="p/g", owner="worker", now=100, lease_seconds=30)
    assert lease is not None
    assert not outbox.authorize(lease, now=130)
    assert outbox.expire(now=130) == 1
    assert outbox.take(goal_id="p/g", owner="replacement", now=131, lease_seconds=30) is None
    assert controls.pending_count("p/g") == 0
