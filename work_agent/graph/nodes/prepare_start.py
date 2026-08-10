"""兼容别名：启动前消解。子图请用 resolve_pipelines。"""

from __future__ import annotations

from work_agent.graph.nodes.pipeline_ops import resolve_pipelines
from work_agent.graph.state import TestFlowState


def prepare_start(state: TestFlowState) -> dict:
    """
    兼容别名：按 start 消解台账流水线（等价 resolve_pipelines）。

    参数:
        state: 主图状态（至少含 user_input）。

    返回:
        与 resolve_pipelines 相同（pipelines / summary / audit 等）。
    """
    return resolve_pipelines({**dict(state), "ops_kind": "start"})
