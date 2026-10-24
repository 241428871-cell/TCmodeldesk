"""Host-neutral progress queries and live requirement insertion."""

from __future__ import annotations

from typing import Literal, TypedDict

import continuation_clock
import dynamic_plan
import goal_requirements
import stage_store


class InteractionResult(TypedDict):
    """Result of either a read-only query or a durable insertion."""

    interaction: Literal["progress", "requirement"]
    progress: dict[str, str | int | bool]
    requirement: goal_requirements.Requirement | None


class InteractionError(ValueError):
    """A message is not valid for this goal interaction boundary."""


MISSING_GOAL = "目标不存在或不属于当前项目"
CONTROL_MESSAGE = "停止和暂停必须使用目标控制按钮，不能伪装成需求"
NO_ACTIVE_GOAL = "当前项目没有活动目标；请先创建或恢复目标"
AMBIGUOUS_GOAL = "当前项目有多个活动目标；请明确 goal_id"


def resolve_goal(project: str, goal_id: str = "") -> str:
    """Resolve an explicit goal or the project's sole active goal without cross-project guessing."""
    selected = goal_id.strip()
    if selected:
        if stage_store.get_goal(selected, project) is None:
            raise InteractionError(MISSING_GOAL)
        return selected
    active = [
        row["id"]
        for row in stage_store.list_goals(project)
        if row["status"] == stage_store.GOAL_ACTIVE
    ]
    if not active:
        raise InteractionError(NO_ACTIVE_GOAL)
    if len(active) != 1:
        raise InteractionError(AMBIGUOUS_GOAL)
    return str(active[0])


def is_progress_message(message: str) -> bool:
    """Recognize the exact `/btw` command with optional whitespace-delimited text."""
    content = message.strip()
    return content == "/btw" or (
        content.startswith("/btw") and len(content) > 4 and content[4].isspace()
    )


def progress(project: str, goal_id: str) -> dict[str, str | int | bool]:
    """Return a small redacted status view without starting or changing any work."""
    goal = stage_store.get_goal(goal_id, project)
    if goal is None:
        raise InteractionError(MISSING_GOAL)
    plan = dynamic_plan.describe_goal(project, goal_id)
    stages = [
        row
        for row in plan["stages"]
        if not row.get("data", {}).get("dynamic_plan", {}).get("superseded_by")
    ]
    accepted = sum(row.get("status") in {"accepted", "final_accepted"} for row in stages)
    running = sum(
        row.get("status") in {"dispatched", "running", "executor_completed"} for row in stages
    )
    pending = sum(
        row["status"] == "pending" for row in goal_requirements.list_for_goal(project, goal_id)
    )
    clock = continuation_clock.snapshot(project, goal_id)
    blocker = str((clock.get("hold") or {}).get("reason") or "")
    if not blocker and clock.get("user_intent") == "STOP":
        blocker = "用户已停止目标"
    elif not blocker and (clock.get("user_intent") == "USER_PAUSE" or not clock.get("enabled")):
        blocker = "用户已暂停自动推进"
    if blocker:
        next_step = "处理或解除上述阻碍后继续；已有成果和待处理需求保持不变"
    elif pending:
        next_step = "主脑将在安全边界读取待处理需求并修订计划"
    else:
        next_step = (
            "继续当前执行与独立验收"
            if goal["status"] == stage_store.GOAL_ACTIVE
            else "目标已结束"
        )
    return {
        "goal_id": goal_id,
        "title": str(goal["title"]),
        "goal_status": str(goal["status"]),
        "clock_status": str(clock.get("status") or "not_configured"),
        "plan_revision": int(plan["plan_revision"]),
        "stage_count": len(stages),
        "accepted_count": accepted,
        "running_count": running,
        "pending_requirement_count": pending,
        "blocker": blocker,
        "next_step": next_step,
        "read_only": True,
    }


def interact(
    project: str,
    goal_id: str,
    message: str,
    *,
    kind: goal_requirements.RequirementKind = "requirement",
    request_id: str = "",
) -> InteractionResult:
    """Route `/btw` to a read-only view and ordinary text to the durable requirement stream."""
    content = message.strip()
    if is_progress_message(content):
        return {
            "interaction": "progress",
            "progress": progress(project, goal_id),
            "requirement": None,
        }
    if content.startswith(("/stop", "/pause")):
        raise InteractionError(CONTROL_MESSAGE)
    requirement = goal_requirements.insert(
        project, goal_id, content, kind=kind, request_id=request_id
    )
    return {
        "interaction": "requirement",
        "progress": progress(project, goal_id),
        "requirement": requirement,
    }


def interact_current(
    project: str,
    message: str,
    *,
    goal_id: str = "",
    kind: goal_requirements.RequirementKind = "requirement",
    request_id: str = "",
) -> InteractionResult:
    """Interact with an explicit goal or the project's sole active goal."""
    return interact(
        project,
        resolve_goal(project, goal_id),
        message,
        kind=kind,
        request_id=request_id,
    )
