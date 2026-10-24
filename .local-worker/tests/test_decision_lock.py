"""Answer hashing and commit share the native writeback's existing goal authority lock."""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import continuation_clock
import decision_scope
import human_decisions
from plan_contracts import PlanError
from test_decision_scope import Card, answer
from test_decision_scope import branch as branch
from test_decision_scope import goal as goal

if TYPE_CHECKING:
    from sqlite3 import Connection

    import pytest
    from test_decision_scope import Branch


def test_native_writeback_waits_until_human_hash_and_answer_commit(
    branch: Branch, monkeypatch: pytest.MonkeyPatch
) -> None:
    hashed, release, attempted, written = Event(), Event(), Event(), Event()
    original = decision_scope.current_fingerprint

    def hold_after_hash(
        database: Connection, project: str, stored: decision_scope.StoredSubject
    ) -> decision_scope.Record:
        observed = original(database, project, stored)
        hashed.set()
        assert release.wait(5), "Test must release the bounded answer pause"
        return observed

    def writeback() -> str:
        attempted.set()
        with continuation_clock.authority_guard(
            str(branch.project.project),
            branch.project.goal_id,
            branch.card.fingerprint.authority_epoch,
        ):
            _ = (branch.project.project / "target.txt").write_text("Subsequent native writeback")
            written.set()
            records = human_decisions.list_decisions(str(branch.project.project))
            return next(
                Card.model_validate_json(json.dumps(row)).status
                for row in records
                if row["id"] == branch.card.id
            )

    monkeypatch.setattr(decision_scope, "current_fingerprint", hold_after_hash)
    with ThreadPoolExecutor(max_workers=2) as workers:
        answering = workers.submit(answer, branch)
        try:
            assert hashed.wait(5)
            writing = workers.submit(writeback)
            assert attempted.wait(5)
            assert not written.wait(0.1)
            assert (branch.project.project / "target.txt").read_text() == "target"
        finally:
            release.set()
        assert answering.result(timeout=5).status == "answered"
        assert writing.result(timeout=5) == "answered"
    assert written.is_set()


def test_two_human_answers_cannot_both_commit(branch: Branch) -> None:
    def submit(_index: int) -> str:
        try:
            return answer(branch).status
        except PlanError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = tuple(workers.map(submit, range(2)))
    assert sorted(results) == ["answered", "decision_resolved"]
