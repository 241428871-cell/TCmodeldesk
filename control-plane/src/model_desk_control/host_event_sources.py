"""Validate actual task transitions and human answers before recording wakeup evidence."""

import json
from hashlib import sha256
from sqlite3 import Connection
from typing import Final

from pydantic import Json, JsonValue, TypeAdapter

from model_desk_control.database import text_cursor
from model_desk_control.host_events import EventError, GoalEvent, clock, record
from model_desk_control.host_trace_models import LegacyObservation, RecordId, TaskObservation

TERMINAL: Final = frozenset(
    {"executor_completed", "accepted", "final_accepted", "returned", "blocked", "failed"}
)
RECORD: Final = TypeAdapter(dict[str, JsonValue])
MAX_SOURCE_BYTES: Final = 256 * 1024


class StageSource(LegacyObservation):
    """The SQL row remains authoritative for stage membership and its new state."""

    id: RecordId
    goal_id: RecordId
    project: str
    status: str
    task_ids: Json[tuple[RecordId, ...]]


class Epoch(LegacyObservation):
    """A decision retains the exact authorization checked by its answer transaction."""

    authority_epoch: int


def digest(raw: str) -> str:
    """Canonicalize a bounded metadata observation without storing its text in events."""
    if len(raw.encode()) > MAX_SOURCE_BYTES:
        code = "event_source_budget"
        raise EventError(code)
    return sha256(json.dumps(RECORD.validate_json(raw), sort_keys=True).encode()).hexdigest()


def task_changed(
    database: Connection, stage_id: str, task_json: str, review_json: str, *, caused_by: str = ""
) -> None:
    """Only tasks attributed to an original brain round can accelerate that pipeline."""
    task_digest, review_digest = digest(task_json), digest(review_json)
    observed = TaskObservation.model_validate_json(task_json)
    if not observed.goal_round_id or observed.authority_epoch is None:
        return
    row = text_cursor(
        database,
        """SELECT json_object('id',id,'goal_id',goal_id,'project',project,'status',status,
        'task_ids',task_ids) FROM stages WHERE id=?""",
        (stage_id,),
    ).fetchone()
    if row is None:
        code = "event_stage_missing"
        raise EventError(code)
    stage = StageSource.model_validate_json(row[0])
    if stage.status not in TERMINAL:
        return
    if (
        observed.project != stage.project
        or observed.goal_id != stage.goal_id
        or observed.stage_id != stage.id
        or not stage.task_ids
        or stage.task_ids[-1] != observed.id
    ):
        code = "event_task_identity"
        raise EventError(code)
    state = clock(database, stage.project, stage.goal_id)
    if state is None:
        return
    owner = text_cursor(
        database,
        """SELECT goal_id,CAST(json_extract(data,'$.authority_epoch') AS TEXT)
        FROM continuation_intents WHERE id=?""",
        (observed.goal_round_id,),
    ).fetchone()
    if owner is None or owner[0] != stage.goal_id or owner[1] != str(observed.authority_epoch):
        code = "event_task_round_identity"
        raise EventError(code)
    evidence = sha256((task_digest + review_digest).encode()).hexdigest()
    record(
        database,
        GoalEvent(
            project=stage.project,
            goal_id=stage.goal_id,
            authority_epoch=state.authority_epoch if caused_by else observed.authority_epoch,
            kind="task_observed",
            subject_id=observed.id,
            stage_id=stage.id,
            outcome=stage.status,
            evidence_digest=evidence,
        ),
        caused_by=caused_by,
    )


def answered(database: Connection, project: str, goal_id: str, decision_id: str) -> None:
    """Persist alongside the real human answer; this is not itself dispatch permission."""
    if clock(database, project, goal_id) is None:
        return
    row = text_cursor(
        database,
        """SELECT substr(fingerprint,1,262145), json_object('subject_hash',subject_hash,
        'answer',answer,'notes',notes),status FROM human_decisions WHERE goal_id=? AND id=?""",
        (goal_id, decision_id),
    ).fetchone()
    if row is None or row[2] != "answered":
        code = "event_answer_missing"
        raise EventError(code)
    epoch = Epoch.model_validate_json(row[0]).authority_epoch
    record(
        database,
        GoalEvent(
            project=project,
            goal_id=goal_id,
            authority_epoch=epoch,
            kind="human_answered",
            subject_id=decision_id,
            stage_id="",
            outcome="answered",
            evidence_digest=digest(row[1]),
        ),
    )
