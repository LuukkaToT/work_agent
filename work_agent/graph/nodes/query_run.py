"""query_run：兼容入口；子图内请用 query_pipelines。"""

from __future__ import annotations

from work_agent.graph.nodes.pipeline_ops import query_pipelines, resolve_pipelines
from work_agent.graph.state import TestFlowState


def query_run(state: TestFlowState) -> dict:
    """独立调用：先消解再查询（测试/兜底）。"""
    resolved = resolve_pipelines({**dict(state), "ops_kind": "query"})
    if not (resolved.get("pipelines") or []):
        return resolved
    merged = {**dict(state), **resolved, "ops_kind": "query"}
    out = query_pipelines(merged)
    # 保留消解 audit
    audits = list(resolved.get("audit") or []) + list(out.get("audit") or [])
    out["audit"] = audits
    return out
