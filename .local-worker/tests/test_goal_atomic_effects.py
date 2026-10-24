"""Local domain changes and their receipts commit together, retaining current human authority."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

import pytest
from pydantic import BaseModel, ConfigDict

from model_desk_control.database import text_cursor
from model_desk_control.host_effects import EffectError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import continuation_clock
import continuation_store
import goal_effects
import stage_store
from test_goal_action_receipts import GoalRun, apply, proposal
from test_goal_action_receipts import run as run

if TYPE_CHECKING:
    from sqlite3 import Connection

    from goal_effects import Record

    from model_desk_control.host_effects import EffectRequest


class Obligation(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="ignore")
    status: str


class ClockView(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="ignore")
    obligations: tuple[Obligation, ...]


def seed_accepted_stage(run: GoalRun) -> None:
    _ = apply(run, proposal(run, dispatch=False))
    with stage_store.connection(run.scope.project) as database:
        _ = database.execute(
            "UPDATE stages SET status='accepted' WHERE goal_id=?", (run.scope.goal_id,)
        )


def test_goal_completion_replay_does_not_create_another_transition(run: GoalRun) -> None:
    seed_accepted_stage(run)
    effect = goal_effects.prepare(run.scope, "complete", "goal_complete", {"status": "completed"})
    _ = stage_store.set_goal_status(
        run.scope.goal_id, "completed", project=run.scope.project, effect=effect
    )
    repeated = stage_store.set_goal_status(
        run.scope.goal_id, "completed", project=run.scope.project, effect=effect
    )
    assert repeated["status"] == "completed"
    with stage_store.connection(run.scope.project) as database:
        rows = text_cursor(
            database, "SELECT detail FROM stage_log WHERE action='goal_status'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "completed"


def test_old_receipts_cannot_resume_effects_after_human_stop(run: GoalRun) -> None:
    actions = proposal(run)
    _ = apply(run, actions)
    _ = continuation_clock.control(run.scope.project, run.scope.goal_id, "stop")
    with pytest.raises(ValueError, match="STOP"):
        _ = apply(run, actions)
    assert len(run.queued) == 1


def test_completion_receipt_cannot_be_applied_to_another_goal(run: GoalRun) -> None:
    other = stage_store.create_goal("Other goal", "Keep this goal isolated", run.scope.project)
    effect = goal_effects.prepare(run.scope, "complete", "goal_complete", {"status": "completed"})
    with pytest.raises(EffectError, match="effect_domain_scope_or_payload_mismatch"):
        _ = stage_store.set_goal_status(
            other["id"], "completed", project=run.scope.project, effect=effect
        )
    current = stage_store.get_goal(other["id"], run.scope.project)
    assert current is not None
    assert current["status"] == "active"


def test_goal_status_and_receipt_rollback_together_on_commit_failure(
    run: GoalRun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_accepted_stage(run)
    original = goal_effects.save

    def fail_save(
        database: Connection,
        request: EffectRequest | None,
        subject_id: str,
        revision: int | None = None,
    ) -> None:
        original(database, request, subject_id, revision)
        reason = "Fixture rejected goal completion commit"
        raise OSError(reason)

    monkeypatch.setattr(goal_effects, "save", fail_save)
    effect = goal_effects.prepare(run.scope, "complete", "goal_complete", {"status": "completed"})
    with pytest.raises(OSError, match="Fixture rejected goal completion commit"):
        _ = stage_store.set_goal_status(
            run.scope.goal_id, "completed", project=run.scope.project, effect=effect
        )
    current = stage_store.get_goal(run.scope.goal_id, run.scope.project)
    assert current is not None
    assert current["status"] == "active"
    with stage_store.connection(run.scope.project) as database:
        rows = text_cursor(
            database, "SELECT request_id FROM brain_requests WHERE kind='goal_complete'"
        ).fetchall()
    assert rows == []


def test_obligation_update_and_receipt_rollback_together_on_clock_write_failure(
    run: GoalRun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = continuation_clock.configure_clock(
        run.scope.project,
        run.scope.goal_id,
        enabled=True,
        obligations=[{"id": "save", "title": "Save the requested artifact"}],
    )
    original = continuation_store.write

    def fail_write(database: Connection, clock: Record) -> None:
        original(database, clock)
        reason = "Fixture rejected obligation commit"
        raise OSError(reason)

    monkeypatch.setattr(continuation_store, "write", fail_write)
    body: Record = {
        "id": "save",
        "status": "fulfilled",
        "evidence": "Inspected saved fixture output",
    }
    effect = goal_effects.prepare(run.scope, "obligation:save", "goal_obligation", body)
    with pytest.raises(OSError, match="Fixture rejected obligation commit"):
        _ = continuation_clock.control(
            run.scope.project, run.scope.goal_id, "obligation", body, effect=effect
        )
    with stage_store.connection(run.scope.project) as database:
        clock_row = text_cursor(
            database, "SELECT data FROM continuation_clocks WHERE goal_id=?", (run.scope.goal_id,)
        ).fetchone()
        rows = text_cursor(
            database, "SELECT request_id FROM brain_requests WHERE kind='goal_obligation'"
        ).fetchall()
    assert clock_row is not None
    assert ClockView.model_validate_json(clock_row[0]).obligations[0].status == "pending"
    assert rows == []


def test_replayed_obligation_does_not_write_a_new_clock_generation(
    run: GoalRun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = continuation_clock.configure_clock(
        run.scope.project,
        run.scope.goal_id,
        enabled=True,
        obligations=[{"id": "save", "title": "Save the requested artifact"}],
    )
    body: Record = {"id": "save", "status": "fulfilled", "evidence": "Inspected saved output"}
    effect = goal_effects.prepare(run.scope, "obligation:save", "goal_obligation", body)
    _ = continuation_clock.control(
        run.scope.project, run.scope.goal_id, "obligation", body, effect=effect
    )
    with stage_store.connection(run.scope.project) as database:
        before = text_cursor(
            database, "SELECT data FROM continuation_clocks WHERE goal_id=?", (run.scope.goal_id,)
        ).fetchone()

    def later_iso(_value: float | None = None) -> str:
        return "2100-01-01T00:00:00+00:00"

    monkeypatch.setattr(continuation_store, "iso", later_iso)
    _ = continuation_clock.control(
        run.scope.project, run.scope.goal_id, "obligation", body, effect=effect
    )
    with stage_store.connection(run.scope.project) as database:
        after = text_cursor(
            database, "SELECT data FROM continuation_clocks WHERE goal_id=?", (run.scope.goal_id,)
        ).fetchone()
    assert before is not None
    assert after == before


@pytest.mark.parametrize(
    ("statement", "value", "reason"),
    [
        (
            "UPDATE brain_requests SET receipt=? WHERE kind='goal_plan'",
            "{}",
            "effect_receipt_invalid",
        ),
        (
            "UPDATE brain_requests SET state=? WHERE kind='goal_plan'",
            "unknown",
            "atomic_effect_has_uncommitted_reservation",
        ),
        (
            "UPDATE brain_requests SET payload_digest=? WHERE kind='goal_plan'",
            "foreign",
            "effect_payload_conflict",
        ),
    ],
)
def test_corrupt_atomic_receipt_cannot_stand_in_for_a_successful_effect(
    run: GoalRun,
    statement: str,
    value: str,
    reason: str,
) -> None:
    actions = proposal(run, dispatch=False)
    _ = apply(run, actions)
    with stage_store.connection(run.scope.project) as database:
        _ = database.execute(statement, (value,))
    with pytest.raises(EffectError, match=reason):
        _ = apply(run, actions)
