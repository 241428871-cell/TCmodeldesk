"""Real temporary SQLite transactions exercise receipts and competing claimers."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from sqlite3 import OperationalError

import pytest

from model_desk_control.control import ContractError, Control, UserIntent
from model_desk_control.store import ControlStore, StoreError


def store_at(directory: Path) -> ControlStore:
    store = ControlStore(directory / "authority.sqlite")
    store.initialize()
    store.create(Control(goal_id="p/g", due_at=100))
    return store


def test_read_does_not_create_repository(tmp_path: Path) -> None:
    path = tmp_path / "absent.sqlite"
    store = ControlStore(path)
    with pytest.raises(OperationalError):
        _ = store.read("unknown")
    assert not path.exists()


def test_duplicate_command_returns_original_receipt_and_one_intent(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    first = store.claim(goal_id="p/g", request_id="same", epoch=0, revision=0, now=100)
    second = store.claim(goal_id="p/g", request_id="same", epoch=0, revision=0, now=100)
    assert first == second
    assert store.pending_count("p/g") == 1
    with pytest.raises(StoreError, match="request_id_payload_conflict"):
        _ = store.claim(goal_id="p/g", request_id="same", epoch=0, revision=0, now=101)


def test_reopen_preserves_current_control_and_stop_receipt(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    stopped = store.stop(goal_id="p/g", request_id="stop-1")
    reopened = ControlStore(store.path)
    assert reopened.read("p/g") == stopped
    assert reopened.stop(goal_id="p/g", request_id="stop-1") == stopped
    assert stopped.intent is UserIntent.STOP
    with pytest.raises(ContractError, match="stale_authority_or_revision"):
        _ = reopened.claim(goal_id="p/g", request_id="old", epoch=0, revision=0, now=10000)
    assert reopened.pending_count("p/g") == 0


def test_two_connections_cannot_create_two_current_dispatch_intents(tmp_path: Path) -> None:
    store = store_at(tmp_path)

    def compete(request_id: str) -> str:
        try:
            _ = ControlStore(store.path).claim(
                goal_id="p/g",
                request_id=request_id,
                epoch=0,
                revision=0,
                now=100,
            )
        except ContractError as error:
            return error.code
        return "claimed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(compete, ("a", "b")))
    assert sorted(outcomes) == ["claimed", "stale_authority_or_revision"]
    assert store.pending_count("p/g") == 1
    assert store.read("p/g").revision == 1
