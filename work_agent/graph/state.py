"""
图的「共享黑板」：每个节点读当前 state，只返回要改的字段。

LangGraph 会按字段名合并更新：
- 普通字段：后写覆盖前写（如 intent、run_id）
- audit 用自定义 reducer append_audit：默认追加，遇到重置哨兵时只保留本轮
"""

from typing import Annotated, TypedDict

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
    summary: dict  # 结构化汇总，给报告 / 台账 / 程序看

    # --- 对外回复 ---
    reply: str  # respond 生成的自然语言回复，CLI 展示的主体

    # 审计轨迹：见 append_audit
    audit: Annotated[list[dict], append_audit]
