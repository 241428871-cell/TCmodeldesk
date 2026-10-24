"""Derive per-stage approval subjects from one bounded evidence observation."""

from hashlib import sha256

from model_desk_control.file_authority import clean_relative_files
from model_desk_control.host_flow import HostRecord
from model_desk_control.host_trace_models import RecordId
from model_desk_control.review_artifacts import ReviewArtifacts
from model_desk_control.review_tasks import TaskEvidence


class StagePart(HostRecord):
    """Prehashed stage contents prevent repeated metadata serialization for shared ancestors."""

    stage_id: RecordId
    dependencies: tuple[str, ...]
    files: tuple[str, ...]
    task_ids: tuple[str, ...]
    full_digest: str
    proposal_digest: str


class StageSubject(HostRecord):
    """A consumed task may change its own output and lifecycle, never its proposal or inputs."""

    stage_id: RecordId
    decision_digest: str
    execution_digest: str


class ScopeHashes(HostRecord):
    """Stable ordered content references contain no task text or filesystem paths outside scope."""

    stages: tuple[tuple[str, str], ...]
    files: tuple[tuple[str, str], ...]
    tasks: tuple[tuple[str, str], ...]


def digest(value: HostRecord) -> str:
    """Hash schema-owned, deterministic values."""
    return sha256(value.model_dump_json().encode()).hexdigest()


def subjects(
    stages: tuple[StagePart, ...],
    artifacts: ReviewArtifacts,
    tasks: TaskEvidence,
    selected: tuple[str, ...],
    *,
    execution: bool,
) -> tuple[StageSubject, ...]:
    """Read no files here; split already observed evidence into independently consumable stages."""
    saved = {stage.stage_id: stage for stage in stages}
    file_hashes = {file.path: digest(file) for file in artifacts.files}
    task_hashes = {record.path: digest(record) for record in tasks.records}
    result: list[StageSubject] = []
    for root in sorted(set(selected)):
        pending, closure = [root], set[str]()
        while pending:
            stage_id = pending.pop()
            if stage_id not in closure:
                closure.add(stage_id)
                pending.extend(saved[stage_id].dependencies)

        def scope(ids: set[str], root_id: str, *, consumed: bool) -> str:
            resources = ids - {root_id} if consumed else ids
            names = clean_relative_files(
                tuple(path for stage_id in sorted(resources) for path in saved[stage_id].files)
            )
            task_ids = {task for stage_id in resources for task in saved[stage_id].task_ids}
            return digest(
                ScopeHashes(
                    stages=tuple(
                        (
                            stage_id,
                            saved[stage_id].proposal_digest
                            if consumed and stage_id == root_id
                            else saved[stage_id].full_digest,
                        )
                        for stage_id in sorted(ids)
                    ),
                    files=tuple((name, file_hashes[name]) for name in sorted(names)),
                    tasks=tuple(
                        (path, task_hashes[path])
                        for path in sorted(task_hashes)
                        if path.split("/")[2] in task_ids
                    ),
                )
            )

        result.append(
            StageSubject(
                stage_id=root,
                decision_digest="" if execution else scope(closure, root, consumed=False),
                execution_digest=scope(closure, root, consumed=True),
            )
        )
    return tuple(result)
