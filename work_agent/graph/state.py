"""
图的「共享黑板」：每个节点读当前 state，只返回要改的字段。

字段分两层：
- 会话级：messages —— 跨轮累积，永不重置，靠 checkpointer 持久化
- 任务级：其余字段 —— 每轮由 intake 归零（下一阶段实现）

LangGraph 合并规则：
- 普通字段：后写覆盖前写（如 intent、run_id）
- messages 用 add_messages：按消息 ID 追加 / 去重
- audit 用 append_audit：默认追加，遇到重置哨兵时只保留本轮

执行流水线私有字段（poll_count / cases / exec_decision）不在顶层，
见 subgraphs/exec_flow.py 的 ExecFlowState（后续步骤拆入）。
"""

from typing import Annotated, TypedDict

from langgraph.graph.message import add_messages
from langchain_core.messages import AnyMessage

# 审计重置哨兵的键名。intake 每轮发一条 {RESET_AUDIT: True}
RESET_AUDIT = "__reset__"


def append_audit(
    old: list[dict] | None,
    new: list[dict] | None,
) -> list[dict]:
    """
    审计轨迹的合并规则。

    默认「只追加」，这样每个节点只需 return {"audit": [自己那一条]}。

    但同一个 thread 会跨多轮对话复用（CLI 里 -t 固定 thread_id 就是这样），
    纯追加会让 audit 越滚越长，第三轮就分不清哪几条属于本轮。
    所以约定：intake 作为每轮入口额外发一条哨兵，reducer 见到哨兵就丢掉历史。
    """
    old = old or []
    new = new or []
    if any(isinstance(rec, dict) and rec.get(RESET_AUDIT) for rec in new):
        return [
            rec
            for rec in new
            if not (isinstance(rec, dict) and rec.get(RESET_AUDIT))
        ]
    return old + new


class TestFlowState(TypedDict):
    # --- 会话级（跨轮累积，intake 不重置）---
    messages: Annotated[list[AnyMessage], add_messages]

    # --- 任务级：会话 / 路由 ---
    task_id: str  # 本次任务 id，落盘报告时用
    user_input: str  # 本轮用户输入（从 messages[-1] 提取）
    intent: str  # router 分类：analysis | execute | query | chat
    requirement: str  # 预留：结构化需求（目前先等于 user_input）
    analysis_path: str  # 测试分析 markdown 落盘路径

    # --- 任务级：执行产出（子图 output 写回；私有字段不在这里）---
    exec_params: dict  # {case_names, version, topology}
    run_id: str  # Executor.run 返回的任务号；空表示还没提交
    run_status: str  # pending | running | finished | failed | ...

    # --- 任务级：结果与报告 ---
    results: list[dict]  # 用例级结果列表
    logs: str  # 原始日志文本
    report_path: str  # report.md 的绝对路径
    summary: dict  # 汇总量：status / total / passed / failed…（不含引用性字段）

    # --- 任务级：对外回复 ---
    reply: str  # respond 生成的自然语言回复，CLI 展示的主体

    # 审计轨迹：见 append_audit（本轮由 intake 哨兵重置）
    audit: Annotated[list[dict], append_audit]
