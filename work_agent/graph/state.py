import operator
from typing import Annotated, TypedDict


class TestFlowState(TypedDict):
    task_id: str
    user_input: str
    intent: str
    requirement: str
    cases: list[dict]
    exec_params: dict  # case_names / version / topology
    run_id: str
    run_status: str
    summary: dict
    audit: Annotated[list[dict], operator.add]