"""
权限规则集中表：谁只读、谁需要人工确认，一处定义，一处审计。

背景：以前「create/start 需要确认」「diagnose 白名单只读」是散落在各处的
手写约定（`diagnose_tools.py` 只手工注册只读函数、`hitl.py` 手写
`interrupt()`），没有一份可测试的集中定义。以后新加 action 容易漏掉该不该确认，或者不小心把写操作混进只读白名单。

这不是一个运行时权限判断引擎（不做 RBAC/动态规则），只是把"事实"收拢成一份
可读、可测试的表，配合 `assert_read_only_whitelist` 在构造期自检。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ActionPolicy:
    """单个 action 的权限声明：是否只读、是否需要人工确认。"""

    name: str
    read_only: bool
    requires_confirmation: bool
    description: str = ""


POLICIES: dict[str, ActionPolicy] = {
    "create_pipeline": ActionPolicy(
        "create_pipeline",
        read_only=False,
        requires_confirmation=True,
        description="创建流水线",
    ),
    "start_pipeline": ActionPolicy(
        "start_pipeline",
        read_only=False,
        requires_confirmation=True,
        description="启动流水线",
    ),
    "query_pipeline": ActionPolicy(
        "query_pipeline", read_only=True, requires_confirmation=False
    ),
    "get_pipeline_status": ActionPolicy(
        "get_pipeline_status", read_only=True, requires_confirmation=False
    ),
    "fetch_logs": ActionPolicy(
        "fetch_logs", read_only=True, requires_confirmation=False
    ),
    "list_log_files": ActionPolicy(
        "list_log_files", read_only=True, requires_confirmation=False
    ),
    "grep_logs": ActionPolicy(
        "grep_logs", read_only=True, requires_confirmation=False
    ),
    "lookup_error_code": ActionPolicy(
        "lookup_error_code", read_only=True, requires_confirmation=False
    ),
    "find_case_history": ActionPolicy(
        "find_case_history", read_only=True, requires_confirmation=False
    ),
    "get_case_spec": ActionPolicy(
        "get_case_spec", read_only=True, requires_confirmation=False
    ),
    "search_knowledge": ActionPolicy(
        "search_knowledge", read_only=True, requires_confirmation=False
    ),
    "fetch_archived_block": ActionPolicy(
        "fetch_archived_block",
        read_only=True,
        requires_confirmation=False,
        description="按 artifact 引用回读已归档的历史上下文原文",
    ),
}


def assert_read_only_whitelist(tool_names: list[str]) -> None:
    """
    校验一批工具名全部已在 ``POLICIES`` 注册且 ``read_only=True``。

    用于诊断白名单构造期自检：误把写操作工具加进只读白名单时，启动期
    （首次调用 ``build_diagnose_tools()``）就直接拦住，不用等到运行时才发现。

    参数:
        tool_names: 待校验的工具名列表。

    异常:
        ValueError: 任一名字未注册，或注册了但 ``read_only=False``。
    """
    for name in tool_names:
        policy = POLICIES.get(name)
        if policy is None:
            raise ValueError(f"工具 {name!r} 未在 POLICIES 注册")
        if not policy.read_only:
            raise ValueError(f"工具 {name!r} 非只读（read_only=False），禁止进入只读白名单")
