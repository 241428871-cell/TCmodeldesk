"""Human owner handoff uses the original project transaction and no new driver."""

from collections.abc import Callable
from pathlib import Path
from typing import Final

import continuation_store
import control_bootstrap
import lease_guard
from pydantic import JsonValue, TypeAdapter

RECORD: Final = TypeAdapter(dict[str, JsonValue])


def handoff(
    project: str,
    goal_id: str,
    payload: dict[str, JsonValue],
    validate_model: Callable[[], None],
) -> dict[str, JsonValue]:
    """The normal authenticated human route supplies project and model validation."""
    control_bootstrap.bind_source()
    from model_desk_control.host_handoff import HandoffRequest, transfer

    request = HandoffRequest.model_validate(
        {
            key: value
            for key, value in payload.items()
            if key not in {"project", "goal_id", "action"}
        }
    )
    root = str(Path(project).resolve(strict=True))

    def validate() -> None:
        reason = lease_guard.goal_reason(root, goal_id)
        if reason:
            from model_desk_control.durable_lease_models import LeaseStoreError

            raise LeaseStoreError(reason)
        validate_model()

    with continuation_store.mutation(root, goal_id) as database:
        receipt = transfer(database, root, goal_id, request, validate)
    return RECORD.validate_json(receipt.model_dump_json())
