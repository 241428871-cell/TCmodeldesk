from typing import TypedDict

class DynamicPlanData(TypedDict, total=False):
    superseded_by: str

class StageData(TypedDict, total=False):
    dynamic_plan: DynamicPlanData

class StageView(TypedDict, total=False):
    status: str
    data: StageData

class PlanView(TypedDict):
    plan_revision: int
    stages: list[StageView]

def describe_goal(project: str, goal_id: str) -> PlanView: ...
