"""
图的「共享黑板」：每个节点读当前 state，只返回要改的字段。

LangGraph 会按字段名合并更新：
- 普通字段：后写覆盖前写（如 intent、run_id）
- audit 用了 Annotated[..., operator.add]：新列表会「追加」到旧列表，不会覆盖
"""

import operator
from typing import Annotated, TypedDict


class TestFlowState(TypedDict):
    # --- 会话 / 路由 ---
    task_id: str  # 本次任务 id，落盘报告时用
    user_input: str  # 用户原始输入
    intent: str  # router 分类结果：analysis | execute | query | chat
    requirement: str  # 预留：结构化需求（目前先等于 user_input）
    analysis_path: str  # 测试分析 markdown 落盘路径

    # --- 执行参数与用例 ---
    cases: list[dict]  # 从 CaseProvider 拉到的用例摘要
    exec_params: dict  # {case_names, version, topology}

    # --- 执行过程 ---
    run_id: str  # Executor.run 返回的任务号；空表示还没提交
    run_status: str  # pending | running | finished | failed | ...
    poll_count: int  # 轮询了几次 status（自循环的刹车计数）

    # --- 结果与报告 ---
    results: list[dict]  # 用例级结果列表
    logs: str  # 原始日志文本
    report_path: str  # report.md 的绝对路径
    summary: dict  # 给 CLI / 报告用的汇总信息

    # 审计轨迹：只追加。节点应 return {"audit": [一条记录]}，不要自己 append 原列表
    audit: Annotated[list[dict], operator.add]
