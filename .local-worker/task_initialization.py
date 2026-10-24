"""Finish original admitted task initialization through the existing goal recovery path."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import continuation_store
import control_bootstrap
import governance_context
import model_registry as registry
import project_storage
import stage_store
import task_initialization_parent
import task_queue
from pydantic import JsonValue, TypeAdapter

if TYPE_CHECKING:
    from collections.abc import Callable
    from sqlite3 import Connection

    from model_desk_control.host_initialization_models import InitialDraft, InitialKey

type Record = dict[str, JsonValue]


class ProjectStorage(Protocol):
    """Only update the original derived task index, never another project's locator."""

    register_task_record: Callable[[str, str, str, str, str, str], None]


def storage() -> ProjectStorage:
    """The existing implementation remains the source of project registration semantics."""
    return project_storage


def create(state: Record, prompt: str, mode: str, rework_parent_id: str = "") -> Record:
    """Admit all resolved inputs with the stage claim before any task or parent file changes."""
    control_bootstrap.bind_source()
    from model_desk_control.host_initialization_models import InitialDraft, InitializationError
    from model_desk_control.host_queue_models import ticket

    initial = {**state, "updated_at": state["created_at"]}
    raw = json.dumps(initial, ensure_ascii=False, sort_keys=True)
    reference = ticket(raw, prompt)
    proof = (
        task_initialization_parent.snapshot(reference, rework_parent_id)
        if rework_parent_id
        else task_initialization_parent.ParentProof("", "")
    )
    draft = InitialDraft(
        ticket=reference,
        task_json=raw,
        prompt=prompt,
        rework_parent_id=rework_parent_id,
        parent_fingerprint=proof.task,
        review_fingerprint=proof.review,
    )
    draft.verify()
    _ = stage_store.claim_dispatch(
        reference.stage_id,
        reference.task_id,
        mode=mode,
        project=reference.project,
        initialization=draft,
    )
    try:
        return materialize(draft)
    except governance_context.DispatchBlocked as error:
        code = "initialization_authority_changed_after_admission"
        raise InitializationError(code) from error


def materialize(draft: InitialDraft) -> Record:
    """Fill only absent files from the original manifest; existing conflicting bytes survive."""
    control_bootstrap.bind_source()
    from model_desk_control.host_initialization_files import publish
    from model_desk_control.host_initialization_models import InitializationError, InitialPhase
    from model_desk_control.host_initialization_store import read
    from model_desk_control.host_queue_store import origin
    from model_desk_control.native_output import owned_path

    draft.verify()
    reference = draft.ticket
    state = TypeAdapter(Record).validate_json(draft.task_json)
    root = Path(reference.project)
    relative = Path(".model-desk/runs") / reference.task_id
    with governance_context.launch_authority(state):
        with continuation_store.readonly(reference.project) as database:
            saved = read(database, reference.task_id) if database else None
            if database is None or saved is None or saved.draft != draft:
                code = "initialization_admission_missing"
                raise InitializationError(code)
            origin(database, reference)
            if saved.phase == InitialPhase.READY:
                return registry.read_task(owned_path(root, relative))
        if owned_path(root, relative / "cancelled.flag").exists():
            code = "initialization_task_cancelled"
            raise InitializationError(code)
        task_initialization_parent.consume(draft)
        directory = registry.prepare_task_dir(reference.task_id, reference.project)
        if directory != owned_path(root, relative):
            code = "initialization_directory_identity"
            raise InitializationError(code)
        publish(root, relative / "prompt.txt", draft.prompt)
        publish(root, relative / "task.json", draft.task_json)
        storage().register_task_record(
            reference.project,
            reference.task_id,
            reference.goal_id,
            reference.stage_id,
            str(state["kind"]),
            str(state["created_at"]),
        )
        task_queue.prepare(state)
    return registry.read_task(directory)


def recover(key: InitialKey) -> None:
    """The original action reconciler may finish local initialization, never launch a model."""
    control_bootstrap.bind_source()
    from model_desk_control.host_initialization_models import InitializationError, InitialPhase
    from model_desk_control.host_initialization_store import find

    with continuation_store.readonly(key.project) as database:
        saved = find(database, key) if database else None
    if saved is not None and saved.phase == InitialPhase.PENDING:
        try:
            _ = materialize(saved.draft)
        except OSError as error:
            code = "initialization_io_incomplete"
            raise InitializationError(code) from error


def retained(database: Connection, stage: Record) -> bool:
    """Startup preserves a claimed stage whose pending manifest proves no queue was prepared."""
    control_bootstrap.bind_source()
    from model_desk_control.host_initialization_models import InitialPhase
    from model_desk_control.host_initialization_store import read

    try:
        record = read(database, str(stage.get("active_task_id") or ""))
        return (
            record is not None
            and record.phase == InitialPhase.PENDING
            and record.draft.ticket.project == stage.get("project")
            and record.draft.ticket.goal_id == stage.get("goal_id")
            and record.draft.ticket.stage_id == stage.get("id")
        )
    except ValueError:
        return False
