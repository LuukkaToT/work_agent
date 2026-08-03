"""消解台账中的流水线并写入 state.pipelines，供 start_pipelines 使用。"""

from __future__ import annotations

from work_agent.graph.nodes.pipeline_resolve import (
    pick_records_for_action,
    records_to_pipeline_dicts,
)
from work_agent.graph.state import TestFlowState


def prepare_start(state: TestFlowState) -> dict:
    user_input = state.get("user_input") or ""
    records = pick_records_for_action(user_input, action="启动")

    if not records:
        return {
            "pipelines": [],
            "summary": {
                "status": "not_found",
                "message": "台账里没有找到可启动的流水线",
            },
            "audit": [{"step": "prepare_start", "status": "not_found"}],
        }

    pipelines = records_to_pipeline_dicts(records)
    return {
        "pipelines": pipelines,
        "audit": [
            {
                "step": "prepare_start",
                "pipeline_ids": [p["pipeline_id"] for p in pipelines],
            }
        ],
    }
