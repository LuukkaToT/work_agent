import operator
from typing import Annotated, TypedDict


class TestFlowState(TypedDict):
    task_id:str
    requirement:str
    summary:dict
    audit: Annotated[list[dict], operator.add]