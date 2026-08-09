"""兼容别名：启动前消解。子图请用 resolve_pipelines。"""

from __future__ import annotations

from work_agent.graph.nodes.pipeline_ops import resolve_pipelines
from work_agent.graph.state import TestFlowState


def prepare_start(state: TestFlowState) -> dict:
    return resolve_pipelines({**dict(state), "ops_kind": "start"})
