"""Human disposition of an observed original round in the existing authority database."""

import json
from datetime import UTC, datetime
from pathlib import Path
from sqlite3 import Connection
from typing import Final

from pydantic import TypeAdapter

from model_desk_control.database import text_cursor
from model_desk_control.host_resolution_models import (
    ProcessState,
    Record,
    ResolutionCommit,
    ResolutionError,
    ResolutionFacts,
    ResolutionInput,
    ResolutionReceipt,
    ResolutionRecords,
    ResolutionView,
)
from model_desk_control.host_resolution_records import (
    JournalFacts,
    digest,
    has_table,
    journal,
    load,
    reference,
)
from model_desk_control.host_resolution_tasks import ProcessProbe, stages, tasks

MAX_DISPLAY_FILES: Final = 100
RECORD: Final = TypeAdapter(Record)


def _main_process(
    records: ResolutionRecords, saved: JournalFacts, probe: ProcessProbe
) -> ProcessState:
    intent = records.parsed_intent
    if intent.pid is not None and intent.birth is not None and intent.pid > 0 and intent.birth > 0:
        return probe(intent.pid, intent.birth)
    if intent.pid is not None or intent.birth is not None:
        return "unknown"
    if (
        not saved.requests
        and not any(p.step in {"native", "actions", "settle"} for p in saved.phases)
        and (intent.host_protocol == "graph_v2" or any(p.step == "snapshot" for p in saved.phases))
    ):
        return "not_started"
    return "unknown"


def _revision(database: Connection, goal_id: str) -> tuple[int, int]:
    plan = (
        text_cursor(
            database,
            """SELECT CAST(COALESCE(MAX(revision),0) AS TEXT)
        FROM plan_revisions WHERE goal_id=?""",
            (goal_id,),
        ).fetchone()
        if has_table(database, "plan_revisions")
        else None
    )
    contract = (
        text_cursor(
            database,
            "SELECT CAST(revision AS TEXT) FROM goal_contracts WHERE goal_id=?",
            (goal_id,),
        ).fetchone()
        if has_table(database, "goal_contracts")
        else None
    )
    return (0 if plan is None else int(plan[0]), 0 if contract is None else int(contract[0]))


def _view(
    database: Connection, project: Path, records: ResolutionRecords, probe: ProcessProbe
) -> ResolutionView:
    scope = reference(records, project)
    clock, intent = records.parsed_clock, records.parsed_intent
    saved = journal(database, scope)
    related = stages(database, project, scope.goal_id)
    task_ids = tuple(
        dict.fromkeys([*related.task_ids, *(r.task_id for r in saved.requests if r.task_id)])
    )
    workers = tasks(project, scope.goal_id, task_ids, clock.authority_epoch, probe)
    process = _main_process(records, saved, probe)
    plan_revision, contract_revision = _revision(database, scope.goal_id)
    facts = ResolutionFacts(
        scope=scope,
        authority_epoch=clock.authority_epoch,
        goal_status=records.goal_status,
        user_intent=clock.user_intent,
        enabled=clock.enabled,
        clock_digest=digest(json.dumps(records.clock, sort_keys=True)),
        intent_digest=digest(json.dumps(records.intent, sort_keys=True)),
        stage_digest=related.digest,
        plan_revision=plan_revision,
        contract_revision=contract_revision,
        phases=saved.phases,
        requests=saved.requests,
        tasks=workers,
        process_state=process,
    )
    blockers = [*saved.blockers, *related.blockers, *(t.blocker for t in workers if t.blocker)]
    if clock.closed or clock.active_intent_id != intent.id:
        blockers.append("原轮次已不再持有此目标，不能用旧事项结束当前工作")
    if intent.status not in {"unknown", "revoked"} and clock.hold.get("kind") != "unknown":
        blockers.append("当前轮次仍在正常处理，不需要人工结束")
    if process not in {"exited", "not_started"}:
        blockers.append("原主脑进程仍在运行或缺少可核对的退出身份")
    return ResolutionView(
        facts=facts,
        subject_hash=digest(facts.model_dump_json()),
        can_resolve=not blockers,
        blockers=tuple(dict.fromkeys(blockers)),
        declared_files=related.files[:MAX_DISPLAY_FILES],
        declared_files_omitted=max(0, len(related.files) - MAX_DISPLAY_FILES),
    )


def inspect(
    database: Connection, project: Path, goal_id: str, probe: ProcessProbe
) -> ResolutionView:
    """Read only an exact project's current original round; no clock is initialized."""
    return _view(database, project, load(database, project, goal_id), probe)


def _receipt(records: ResolutionRecords, request: ResolutionInput) -> ResolutionReceipt | None:
    saved = records.parsed_intent.resolution
    if saved is None:
        return None
    receipt = ResolutionReceipt.model_validate(saved)
    if (
        receipt.input_digest != digest(request.model_dump_json())
        or receipt.intent_id != request.intent_id
        or receipt.original_owner_id != records.parsed_intent.owner_id
        or receipt.scope != reference(records, Path(records.parsed_clock.project))
        or receipt.subject_hash != request.subject_hash
        or receipt.authority_epoch != request.authority_epoch
        or receipt.resulting_epoch != request.authority_epoch + 1
        or records.parsed_intent.status != "human_reconciled"
    ):
        code = "resolution_completed_input_conflict"
        raise ResolutionError(code)
    return receipt


def resolve(
    database: Connection, project: Path, goal_id: str, request: ResolutionInput, probe: ProcessProbe
) -> ResolutionCommit:
    """Caller holds original graph and goal locks; clock and intent change atomically."""
    records = load(database, project, goal_id, request.intent_id)
    existing = _receipt(records, request)
    if existing is not None:
        return ResolutionCommit(existing, replayed=True)
    current = _view(database, project, records, probe)
    clock, intent = records.parsed_clock, records.parsed_intent
    if (
        request.subject_hash != current.subject_hash
        or request.authority_epoch != clock.authority_epoch
    ):
        code = "resolution_evidence_changed"
        raise ResolutionError(code)
    if not current.can_resolve:
        code = "resolution_incomplete_evidence"
        raise ResolutionError(code)
    now = datetime.now(UTC).isoformat()
    receipt = ResolutionReceipt(
        scope=current.facts.scope,
        intent_id=intent.id,
        original_owner_id=intent.owner_id,
        subject_hash=current.subject_hash,
        input_digest=digest(request.model_dump_json()),
        notes=request.notes,
        at=now,
        authority_epoch=clock.authority_epoch,
        resulting_epoch=clock.authority_epoch + 1,
    )
    document = RECORD.validate_json(receipt.model_dump_json())
    records.intent.update(status="human_reconciled", resolution=document)
    records.clock.update(
        active_intent_id="",
        authority_epoch=receipt.resulting_epoch,
        last_resolution=document,
        updated_at=now,
    )
    if clock.hold.get("kind") == "unknown" and clock.hold.get("guard_id", "") in {"", intent.id}:
        records.clock["hold"] = {}
    _ = database.execute(
        """UPDATE continuation_intents SET status='human_reconciled',data=?
        WHERE id=? AND goal_id=?""",
        (json.dumps(records.intent, ensure_ascii=False), intent.id, goal_id),
    )
    _ = database.execute(
        "UPDATE continuation_clocks SET data=? WHERE goal_id=?",
        (json.dumps(records.clock, ensure_ascii=False), goal_id),
    )
    return ResolutionCommit(receipt, replayed=False)
