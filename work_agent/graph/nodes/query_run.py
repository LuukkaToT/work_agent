"""query_run：兼容入口；子图内请用 query_pipelines。"""

from __future__ import annotations

from work_agent.graph.nodes.pipeline_ops import query_pipelines, resolve_pipelines
from work_agent.graph.state import TestFlowState


def query_run(state: TestFlowState) -> dict:
    """
    独立调用入口：先消解再查询（测试/兜底；子图内请用 query_pipelines）。

    参数:
        state: 主图状态（至少含 user_input）。

    返回:
        消解失败时返回消解结果；成功则合并 query_pipelines 产出并串联 audit。
    """
    resolved = resolve_pipelines({**dict(state), "ops_kind": "query"})
    if not (resolved.get("pipelines") or []):
        return resolved
    merged = {**dict(state), **resolved, "ops_kind": "query"}
    out = query_pipelines(merged)
    # 保留消解 audit
    audits = list(resolved.get("audit") or []) + list(out.get("audit") or [])
    out["audit"] = audits
    return out
