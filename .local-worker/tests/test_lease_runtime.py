"""Durable host boundaries: loss, identity, orphan recovery and atomic takeover."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import psutil
import pytest
from model_desk_control import durable_leases as leases
from model_desk_control.durable_lease_models import LeasePhase, LeaseStoreError
from model_desk_control.host_queue_models import Owner, Process
from model_desk_control.resource_leases import LeaseRequest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lease_processes
import lease_runtime
import lease_storage
import model_registry as registry


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(registry, "HERE", tmp_path)
    return tmp_path


def request(home: Path, identity: str = "original") -> LeaseRequest:
    return LeaseRequest(owner=identity, project=str(home), account="fixture", files=("a.py",))


def departed() -> Owner:
    return Owner(instance="exited", process=Process(pid=2147483647, birth=1.0))


def test_readonly_status_does_not_create_a_store(home: Path) -> None:
    # Given an untouched installation.
    before = tuple(home.iterdir())
    # When inspecting the resource store.
    with lease_storage.readonly() as database:
        assert database is None
    # Then viewing status created no database, marker or lock.
    assert tuple(home.iterdir()) == before


def test_new_store_syncs_identity_marker_directory_before_use(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a fresh installation whose marker directory sync is observable.
    synced: list[Path] = []
    monkeypatch.setattr(lease_storage, "sync_directory", synced.append)
    # When the first durable reservation initializes the shared store.
    receipt = lease_runtime.reserve(request(home))
    # Then every publication phase persisted its parent before admission returned.
    assert receipt is not None
    assert len(synced) >= 3
    assert set(synced) == {home}


def test_initialization_resumes_after_marker_publication(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given the controller stopped after fencing the store but before publishing its database.
    initialize = lease_storage._initialize_database
    monkeypatch.setattr(
        lease_storage,
        "_initialize_database",
        lambda _target, _identity: (_ for _ in ()).throw(RuntimeError("interrupted")),
    )
    with pytest.raises(RuntimeError, match="interrupted"):
        _ = lease_runtime.reserve(request(home))
    marker = home / "resource-leases.db.identity"
    assert marker.read_text(encoding="ascii").startswith("initializing:")
    assert not lease_storage.path().exists()

    # When the same installation retries under its process lock.
    monkeypatch.setattr(lease_storage, "_initialize_database", initialize)
    receipt = lease_runtime.reserve(request(home))

    # Then it finishes the fenced initialization instead of treating the store as lost.
    assert receipt is not None
    assert marker.read_text(encoding="ascii").startswith("ready:")


def test_initialization_resumes_after_database_publication(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given the complete database was published but its ready marker was interrupted.
    publish = lease_storage._publish_marker

    def interrupt_ready(marker: Path, state: str, identity: str) -> None:
        if state == lease_storage.READY:
            raise RuntimeError("interrupted")
        publish(marker, state, identity)

    monkeypatch.setattr(lease_storage, "_publish_marker", interrupt_ready)
    with pytest.raises(RuntimeError, match="interrupted"):
        _ = lease_runtime.reserve(request(home))
    marker = home / "resource-leases.db.identity"
    assert lease_storage.path().exists()
    assert marker.read_text(encoding="ascii").startswith("initializing:")
    with pytest.raises(LeaseStoreError, match="lease_store_initializing"), lease_storage.readonly():
        pass

    # When initialization is resumed.
    monkeypatch.setattr(lease_storage, "_publish_marker", publish)
    receipt = lease_runtime.reserve(request(home))

    # Then the matching database is promoted and used without replacement.
    assert receipt is not None
    assert marker.read_text(encoding="ascii").startswith("ready:")


@pytest.mark.parametrize("boundary", ["before_database", "before_ready"])
def test_initialization_recovers_after_controller_process_exit(home: Path, boundary: str) -> None:
    # Given a separate production caller exits without cleanup at an initialization boundary.
    script = """
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import lease_runtime, lease_storage, model_registry as registry
from model_desk_control.resource_leases import LeaseRequest
registry.HERE = Path(sys.argv[2])
boundary = sys.argv[3]
if boundary == 'before_database':
    lease_storage._initialize_database = lambda *_: os._exit(71)
else:
    publish = lease_storage._publish_marker
    def interrupted(marker, state, identity):
        if state == lease_storage.READY:
            os._exit(71)
        publish(marker, state, identity)
    lease_storage._publish_marker = interrupted
lease_runtime.reserve(LeaseRequest(owner='crashed', project=sys.argv[2], account='fixture'))
"""
    completed = subprocess.run(  # noqa: S603 - fixed interpreter and isolated fixture arguments.
        [
            sys.executable,
            "-c",
            script,
            str(Path(lease_runtime.__file__).parent),
            str(home),
            boundary,
        ],
        check=False,
        timeout=10,
    )
    assert completed.returncode == 71
    marker = home / "resource-leases.db.identity"
    assert marker.read_text(encoding="ascii").startswith("initializing:")
    assert lease_storage.path().exists() is (boundary == "before_ready")

    # When a new controller enters the ordinary production reservation path.
    receipt = lease_runtime.reserve(request(home, "replacement"))

    # Then it owns the recovered ready store; no cleanup from the dead process was required.
    assert receipt is not None
    assert marker.read_text(encoding="ascii").startswith("ready:")


def test_legacy_identity_marker_remains_readable(home: Path) -> None:
    # Given a store created by the previous raw-identity marker format.
    receipt = lease_runtime.reserve(request(home))
    assert receipt is not None
    marker = home / "resource-leases.db.identity"
    marker.write_text(marker.read_text(encoding="ascii").removeprefix("ready:"), encoding="ascii")

    # When the upgraded runtime inspects it.
    with lease_storage.readonly() as database:
        assert database is not None
        assert leases.read(database, receipt.token) == receipt


def test_lost_store_is_not_silently_recreated(home: Path) -> None:
    # Given an active reservation and a lost database, while its identity marker remains.
    assert lease_runtime.reserve(request(home)) is not None
    lease_storage.path().unlink()
    # When another task tries admission.
    with pytest.raises(LeaseStoreError, match="lease_store_missing_or_unrecognized"):
        _ = lease_runtime.reserve(request(home, "second"))
    # Then the missing holds cannot be replaced with an empty resource table.
    assert not lease_storage.path().exists()


@pytest.mark.parametrize("suffix", ["", ".identity", ".lock", "-journal"])
def test_linked_store_components_are_refused(home: Path, suffix: str) -> None:
    # Given a component redirects to an unrelated file.
    target = home / "unrelated"
    _ = target.write_text("preserve")
    (home / ("resource-leases.db" + suffix)).symlink_to(target)
    # When the installation tries admission.
    with pytest.raises(LeaseStoreError, match="lease_store_path_invalid"):
        _ = lease_runtime.reserve(request(home))
    # Then the unrelated file is untouched.
    assert target.read_text() == "preserve"


def test_armed_without_child_receipt_cannot_be_reclaimed(home: Path) -> None:
    # Given the controller died in the Popen acknowledgement gap.
    with lease_storage.transaction() as database:
        old = leases.reserve(database, request(home), departed())
        assert old is not None
        old = leases.arm(database, old)
    # When a new controller requests the same file or attempts recovery.
    assert lease_runtime.reserve(request(home, "second")) is None
    with pytest.raises(LeaseStoreError, match="lease_recovery_not_quiescent"):
        _ = lease_runtime.takeover(old.request, old.token)
    # Then the unknown launch remains fenced.
    assert lease_runtime.current(old.token) == old


def test_unstarted_original_task_can_resume_after_exact_owner_exit(home: Path) -> None:
    # Given an original callback lost its owner before arming.
    with lease_storage.transaction() as database:
        old = leases.reserve(database, request(home), departed())
        assert old is not None
    # When the existing queue redelivers that same original task.
    fresh = lease_runtime.reserve(request(home))
    # Then only a new fenced generation is issued.
    assert fresh is not None
    assert fresh.token != old.token
    assert lease_runtime.current(old.token).phase == LeasePhase.RELEASED


def test_recovery_reservation_metadata_gap_is_reconciled(home: Path) -> None:
    # Given recovery reserved resources but died before saving its token to task.json.
    with lease_storage.transaction() as database:
        original = leases.reserve(database, request(home), departed())
        assert original is not None
        assert leases.reclaim_unstarted(database, original, lambda _: True)
        orphan = leases.reserve(database, request(home), departed())
        assert orphan is not None
    # When explicit file recovery sees the original token again.
    replacement = lease_runtime.takeover(request(home), original.token)
    # Then the departed unstarted recovery is retired without an unlocked write window.
    assert replacement.token not in (original.token, orphan.token)
    assert lease_runtime.current(orphan.token).phase == LeasePhase.RELEASED


def test_failed_takeover_rolls_back_release(home: Path) -> None:
    # Given a retired original writer and a separate live writer occupying its files.
    first = lease_runtime.reserve(request(home))
    assert first is not None
    assert lease_runtime.release(first.token)
    other = lease_runtime.reserve(request(home, "other"))
    assert other is not None
    # When recovery cannot acquire those resources in its exchange transaction.
    with pytest.raises(LeaseStoreError, match="lease_recovery_resources_busy"):
        _ = lease_runtime.takeover(request(home), first.token)
    # Then no extra recovery hold was committed and the competing owner remains unchanged.
    with lease_storage.readonly() as database:
        assert database is not None
        assert leases.active(database) == (other,)


def test_process_inspection_denied_is_not_exit(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a known live identity whose process state is unreadable.
    receipt = lease_runtime.reserve(request(home))
    assert receipt is not None

    def denied(_pid: int) -> psutil.Process:
        raise psutil.AccessDenied(pid=os.getpid())

    monkeypatch.setattr(psutil, "Process", denied)
    # When recovery checks the old holder.
    result = lease_processes.exited(receipt.holder.process)
    # Then UNKNOWN cannot release resources.
    assert result is False
