"""Represent actual historical whole-goal review rows without upgrading their authority."""

from dataclasses import dataclass

import decision_records
import plan_records
import stage_store
from pydantic import JsonValue, TypeAdapter

from model_desk_control.database import text_cursor

type Record = dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class LegacyReview:
    subject_hash: str
    fingerprint: Record


def legacy(project: str, goal_id: str, decision_id: str) -> LegacyReview:
    """Save exactly the legacy payload/fingerprint/hash relationship in a temporary store."""
    with stage_store.connection(project) as database, database:
        row = text_cursor(
            database,
            "SELECT payload FROM human_decisions WHERE id=? AND goal_id=?",
            (decision_id, goal_id),
        ).fetchone()
        assert row is not None
        payload = TypeAdapter(Record).validate_json(row[0])
        fingerprint = decision_records.current_fingerprint(database, project, goal_id)
        payload["artifact_digest"] = fingerprint["artifact_digest"]
        subject = plan_records.digest(
            {"goal_id": goal_id, "proposal": payload, "fingerprint": fingerprint}
        )
        _ = database.execute(
            "UPDATE human_decisions SET payload=?,fingerprint=?,subject_hash=? WHERE id=?",
            (plan_records.dump(payload), plan_records.dump(fingerprint), subject, decision_id),
        )
        return LegacyReview(subject, fingerprint)
