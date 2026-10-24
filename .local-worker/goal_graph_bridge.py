"""LangGraph phase receipts inside the existing project authority; no second goal clock."""

from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import ClassVar, Final, assert_never

import continuation_store
import model_registry as registry
from langgraph.checkpoint.sqlite import SqliteSaver
from pydantic import BaseModel, ConfigDict, JsonValue

from model_desk_control.database import text_cursor
from model_desk_control.host_flow import HostCallbacks, HostRef, HostStep, build_host_flow

SCHEMA: Final = """CREATE TABLE IF NOT EXISTS goal_graph_steps (
 intent_id TEXT NOT NULL, step TEXT NOT NULL, scope_hash TEXT NOT NULL,
 version INTEGER NOT NULL, status TEXT NOT NULL CHECK(status IN ('inflight','done')),
 result_json TEXT CHECK(result_json IS NULL OR json_valid(result_json)),
 PRIMARY KEY(intent_id, step))"""


class GraphPhaseError(RuntimeError):
    """Unresolved effects stay owned; a graph retry cannot blindly repeat them."""

    def __init__(self, code: str) -> None:
        """Expose stable metadata without provider output or user context."""
        self.code: str = code
        super().__init__(code)


class PhaseRevokedError(ValueError):
    """Current STOP or other authority revocation prevents a new phase effect."""

    def __init__(self, code: str) -> None:
        """Keep the machine reason separate from any native output."""
        self.code: str = code
        super().__init__(code)


class Authority(BaseModel):
    """Parse only current authority fields from the existing clock document."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="ignore")
    authority_epoch: int
    active_intent_id: str
    user_intent: str
    enabled: bool
    closed: bool
    hold: dict[str, JsonValue]


class IntentOwner(BaseModel):
    """The effect caller must own the original claimed intent."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="ignore")
    id: str
    goal_id: str
    authority_epoch: int
    owner_id: str


@dataclass(frozen=True, slots=True)
class GoalGraphHost:
    """Explicit execution owns a graph lock; STOP uses its separate goal authority lock."""

    reference: HostRef
    callbacks: HostCallbacks
    replay_actions: bool = False
    replay_settle: bool = False
    enter: Callable[[], bool] | None = None

    def _version(self, step: HostStep) -> int:
        return (
            2
            if (
                (self.replay_actions and step is HostStep.ACTIONS)
                or (self.replay_settle and step is HostStep.SETTLE)
            )
            else 1
        )

    def _scope_hash(self) -> str:
        return sha256(self.reference.model_dump_json().encode()).hexdigest()

    def result(self, step: HostStep) -> str:
        """Read an exact completed phase from the owning project, never from a checkpoint."""
        with continuation_store.mutation(
            self.reference.project, self.reference.goal_id
        ) as database:
            row = text_cursor(
                database,
                """SELECT scope_hash, CAST(version AS TEXT), status, COALESCE(result_json, '')
                   FROM goal_graph_steps WHERE intent_id = ? AND step = ?""",
                (self.reference.operation_id, step.value),
            ).fetchone()
            if (
                row is None
                or row[0] != self._scope_hash()
                or row[1] != str(self._version(step))
                or row[2] != "done"
            ):
                code = "phase_result_not_available"
                raise GraphPhaseError(code)
            return row[3]

    def _begin(self, step: HostStep) -> bool:
        with continuation_store.mutation(
            self.reference.project, self.reference.goal_id
        ) as database:
            _ = database.execute(SCHEMA)
            row = text_cursor(
                database,
                """SELECT scope_hash, CAST(version AS TEXT), status FROM goal_graph_steps
                   WHERE intent_id = ? AND step = ?""",
                (self.reference.operation_id, step.value),
            ).fetchone()
            if row is not None:
                if row[0] != self._scope_hash() or row[1] != str(self._version(step)):
                    code = "phase_scope_or_version_conflict"
                    raise GraphPhaseError(code)
                if row[2] == "done":
                    return False
                if not (
                    (self.replay_actions and step is HostStep.ACTIONS)
                    or (self.replay_settle and step is HostStep.SETTLE)
                ):
                    code = "phase_requires_original_effect_reconciliation"
                    raise GraphPhaseError(code)
            current = text_cursor(
                database,
                "SELECT data FROM continuation_clocks WHERE goal_id = ?",
                (self.reference.goal_id,),
            ).fetchone()
            if current is None:
                code = "phase_authority_missing"
                raise GraphPhaseError(code)
            authority = Authority.model_validate_json(current[0])
            if (
                authority.authority_epoch != self.reference.epoch
                or authority.active_intent_id != self.reference.operation_id
                or authority.user_intent != "RUN"
                or not authority.enabled
                or authority.closed
                or authority.hold
            ):
                code = "phase_authority_revoked"
                raise PhaseRevokedError(code)
            claimed = text_cursor(
                database,
                "SELECT data FROM continuation_intents WHERE id = ? AND goal_id = ?",
                (self.reference.operation_id, self.reference.goal_id),
            ).fetchone()
            if claimed is None:
                code = "phase_intent_missing"
                raise GraphPhaseError(code)
            owner = IntentOwner.model_validate_json(claimed[0])
            if (
                owner.id != self.reference.operation_id
                or owner.goal_id != self.reference.goal_id
                or owner.authority_epoch != self.reference.epoch
                or owner.owner_id != self.reference.owner_id
            ):
                code = "phase_intent_owner_mismatch"
                raise GraphPhaseError(code)
            if row is None:
                _ = database.execute(
                    "INSERT INTO goal_graph_steps VALUES (?, ?, ?, ?, 'inflight', NULL)",
                    (
                        self.reference.operation_id,
                        step.value,
                        self._scope_hash(),
                        self._version(step),
                    ),
                )
            return True

    def run_step(self, step: HostStep, reference: HostRef) -> None:
        """Record effect intent before calling; uncertain phases cannot be retried by re-entry."""
        if reference != self.reference:
            code = "phase_reference_mismatch"
            raise GraphPhaseError(code)
        if not self._begin(step):
            return
        match step:
            case HostStep.SNAPSHOT:
                result = self.callbacks.snapshot()
            case HostStep.NATIVE:
                result = self.callbacks.native()
            case HostStep.ACTIONS:
                result = self.callbacks.actions()
            case HostStep.SETTLE:
                result = self.callbacks.settle()
            case _:
                assert_never(step)
        with continuation_store.mutation(reference.project, reference.goal_id) as database:
            cursor = database.execute(
                """UPDATE goal_graph_steps SET status = 'done', result_json = ?
                   WHERE intent_id = ? AND step = ? AND scope_hash = ? AND status = 'inflight'""",
                (result, reference.operation_id, step.value, self._scope_hash()),
            )
            if cursor.rowcount != 1:
                row = text_cursor(
                    database,
                    """SELECT status,COALESCE(result_json,'') FROM goal_graph_steps
                       WHERE intent_id=? AND step=? AND scope_hash=? AND version=?""",
                    (reference.operation_id, step.value, self._scope_hash(), self._version(step)),
                ).fetchone()
                if row is not None and row[0] == "done" and row[1] == result:
                    return
                code = "phase_receipt_write_conflict"
                raise GraphPhaseError(code)

    def run(self) -> None:
        """Drive the already claimed intent with one persistent graph and no new timer."""
        identity = self._scope_hash()
        path = Path(self.reference.project) / ".model-desk" / "langgraph" / "checkpoints.sqlite"
        path.parent.mkdir(parents=True, exist_ok=True)
        with (
            registry.file_lock(Path(registry.HERE) / ("goal-graph-" + identity + ".lock")),
            SqliteSaver.from_conn_string(str(path)) as saver,
        ):
            if self.enter is not None and not self.enter():
                return
            graph = build_host_flow(self, saver)
            _ = graph.invoke(
                {"reference_json": self.reference.model_dump_json(), "last_step": ""},
                {"configurable": {"thread_id": "native-goal-v1:" + identity}},
                version="v2",
            )
