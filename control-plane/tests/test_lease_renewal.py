"""Renewal fences old controller generations without creating a second continuation."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from model_desk_control.database import StoreError
from model_desk_control.outbox import OutboxStore
from model_desk_control.round_models import RoundStage
from model_desk_control.store import ControlStore
from tests.round_fixtures import leased_goal, prepared_round


def test_renewal_immediately_invalidates_old_expiry_generation(tmp_path: Path) -> None:
    controls, lease = leased_goal(tmp_path)
    outbox = OutboxStore(controls)
    renewed = outbox.renew(lease, now=110, lease_seconds=30)
    assert renewed.expires_at == 140
    assert renewed.operation_id == lease.operation_id
    assert not outbox.authorize(lease, now=111)
    assert outbox.authorize(renewed, now=111)
    assert controls.pending_count(lease.goal_id) == 0


def test_competing_renewals_from_same_old_token_have_one_winner(tmp_path: Path) -> None:
    controls, lease = leased_goal(tmp_path)

    def compete(_identity: str) -> str:
        try:
            _ = OutboxStore(ControlStore(controls.path)).renew(lease, now=110, lease_seconds=30)
        except StoreError as error:
            return error.code
        return "renewed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(compete, ("a", "b")))
    assert sorted(outcomes) == ["lease_not_current", "renewed"]


def test_expired_lease_cannot_be_renewed_as_new_dispatch_authority(tmp_path: Path) -> None:
    controls, lease = leased_goal(tmp_path)
    with pytest.raises(StoreError, match="lease_not_current"):
        _ = OutboxStore(controls).renew(lease, now=130, lease_seconds=30)


def test_old_controller_cannot_cancel_a_renewed_prepared_operation(tmp_path: Path) -> None:
    controls, lease, rounds = prepared_round(tmp_path)
    renewed = OutboxStore(controls).renew(lease, now=110, lease_seconds=30)
    record = rounds.finalize(lease)
    assert record.stage is RoundStage.PREPARED
    assert controls.read(lease.goal_id).busy
    assert OutboxStore(controls).authorize(renewed, now=111)
