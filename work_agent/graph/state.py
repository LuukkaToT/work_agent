"""
图的「共享黑板」：每个节点读当前 state，只返回要改的字段。

字段分两层：
- 会话级：messages / dialogue_summary —— 跨轮累积，永不重置，靠 checkpointer 持久化
- 任务级：其余字段 —— 每轮由 intake 显式归零

LangGraph 合并规则：
- 普通字段：后写覆盖前写（如 intent、pipelines、dialogue_summary）
- messages 用 add_messages：按消息 ID 追加 / 去重 / RemoveMessage 删除
- audit 用 append_audit：默认追加，遇到重置哨兵时只保留本轮

执行流水线私有字段（exec_decision）在
subgraphs/exec_flow.py 的 ExecFlowState 里，不进顶层。
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
    审计轨迹的合并规则（LangGraph reducer）。

    默认「只追加」，这样每个节点只需 return {"audit": [自己那一条]}。
    同一个 thread 跨多轮复用时，纯追加会让 audit 越滚越长；
    约定 intake 每轮发 RESET_AUDIT 哨兵，reducer 见到哨兵就丢掉历史。

    参数:
        old: 已有 audit 列表。
        new: 本节点新写入的 audit 条目（可含哨兵）。

    返回:
        合并后的 audit 列表（哨兵本身不会保留）。
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
    # 滚动摘要：窗口外旧对话的压缩，供 router / exec_params 注入
    dialogue_summary: str
    # 当前操作者工号（CLI 走 core/identity.py 的 EnvIdentityProvider，HTTP 走
    # api/identity.py 的 HeaderIdentityProvider；两者调用形状不同，但落到 state
    # 里统一是这一个字符串）。由 runtime.py 在每次 invoke 时随消息一并写入；
    # intake 不重置——同一个 thread 换轮不该换身份。
    user_id: str

    # --- 任务级：会话 / 路由 ---
    task_id: str  # 本次任务 id
    user_input: str  # 本轮用户输入（从 messages[-1] 提取）
    intent: str  # analysis | execute | query | start | diagnose | chat | set_mode
    requirement: str  # 预留：结构化需求（目前先等于 user_input）
    analysis_path: str  # 测试分析 markdown 落盘路径
    # 个人偏好：调测模式。intake 每轮从 core/user_config.py 重新读取（不是简单
    # 归零），保证换会话/换设备改了配置后，下一轮就能看到新值；intent=set_mode
    # 时由 router 直接改写成目标值，本轮下游立刻生效，见 nodes/set_mode.py。
    debug_mode: bool

    # --- 任务级：执行子图 output 写回 ---
    # {plans: [{case_names, version, env, env_kind, missing}, ...]}
    exec_params: dict
    # [{pipeline_id, case_names, version, env, status, error}, ...]
    pipelines: list[dict]

    # --- 任务级：查询结果 ---
    results: list[dict]  # 用例级结果列表（query_pipelines 聚合）
    # 只放汇总量：status / message / answer / total / passed / failed_count / failed / created / failed_pipelines
    summary: dict

    # --- 任务级：对外回复 ---
    reply: str  # respond 生成的自然语言回复，CLI 展示的主体

    # 审计轨迹：见 append_audit（本轮由 intake 哨兵重置）
    audit: Annotated[list[dict], append_audit]
