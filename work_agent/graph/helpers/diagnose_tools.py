"""
诊断期只读 Tool 工厂（ReAct 加厚核心）。

禁止注册 create / start。
面向用户的 query 仍走 pipeline_ops 图节点；这里的 get_pipeline_status 只是诊断窥视。
"""

from __future__ import annotations

import json
from dataclasses import asdict
from collections.abc import Sequence
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool, tool

from work_agent.core.ledger import get_ledger
from work_agent.core.policy import assert_read_only_whitelist
from work_agent.core.transcript import Transcript
from work_agent.graph.helpers.context_archive import Archive
from work_agent.graph.helpers.truncate import CharBudget
from work_agent.tools.mock.scenarios import MockScenario
from work_agent.tools.registry import (
    get_case_provider,
    get_knowledge_search_tool,
    get_log_tool,
    get_pipeline_tool,
)

_DEFAULT_MAX_CHARS = 8000


def build_diagnose_tools(
    *,
    scenario: MockScenario = "case_error",
    budget: CharBudget | None = None,
    tool_result_max_chars: int = _DEFAULT_MAX_CHARS,
    user_id: str = "",
    archive: Archive | None = None,
    durable: bool = False,
    transcript: Transcript | None = None,
) -> list[BaseTool]:
    """
    构造诊断白名单工具（只读；禁止 create/start）。

    pipeline / log 使用同一 scenario，避免状态与日志对不上。

    参数:
        scenario: mock 故障场景，同时作用于 pipeline 与 log。
        budget: 本轮工具返回累计字符预算；None 用默认。
        tool_result_max_chars: 单次工具结果截断上限。
        user_id: 当前操作者工号；``find_case_history`` 只在其名下记录里查，
            空串表示未接身份（此时按台账回填后的语义查不到任何记录）。
        archive: 归档器；传入时额外注册 ``fetch_archived_block``，
            供模型按 artifact 引用回读被裁剪历史的摘录。None 不注册，
            工具集保持原有 8 个（legacy 基线不变）。
        durable: 在线持久化子图用：跳过出口预算并把异常上抛。轻量 transcript
            不要打开它，否则会同时改变预算和错误语义。
        transcript: 传入时先把截断前原文写入历史流，并让工具返回原文。
            模型输入由 ContextManager 按预算投影；CharBudget 只记账，
            不再把截断结果当成工具返回。None 保持原出口行为。

    返回:
        LangChain BaseTool 列表，供 ReAct agent 使用。
    """
    if budget is None:
        budget = CharBudget(limit=40_000)

    pipeline_tool = get_pipeline_tool(scenario=scenario)
    log_tool = get_log_tool(scenario=scenario)
    case_provider = get_case_provider()
    knowledge = get_knowledge_search_tool()
    ledger = get_ledger()
    max_chars = tool_result_max_chars

    def _out(text: str) -> str:
        """有 transcript 时先落盘原文并返回全文；否则按预算截断工作副本。"""
        if transcript is not None:
            transcript.put_text(text)
            # 只记账，不把截断结果当成工具返回，否则后续归档只能读到已删中间段。
            budget.take(text, max_chars=max_chars)
            return text
        if durable:
            return text
        return budget.take(text, max_chars=max_chars)

    @tool
    def get_pipeline_status(pipeline_id: str) -> str:
        """查看流水线当前 phase 与各用例 verdict（只读）。诊断开始时优先调用。"""
        try:
            pr = pipeline_tool.query(pipeline_id)
            payload = {
                "pipeline_id": pr.pipeline_id,
                "phase": pr.phase,
                "message": pr.message,
                "results": [asdict(r) for r in pr.results],
            }
            return _out(json.dumps(payload, ensure_ascii=False, indent=2))
        except Exception as exc:  # noqa: BLE001
            if durable:
                raise
            return _out(f"[get_pipeline_status error] {exc}")

    @tool
    def list_log_files(pipeline_id: str) -> str:
        """列出流水线的分组件日志文件、组件名、行数和大小；首次看分层日志时调用。"""
        try:
            return _out(log_tool.list_logs(pipeline_id))
        except Exception as exc:  # noqa: BLE001
            if durable:
                raise
            return _out(f"[list_log_files error] {exc}")

    @tool
    def fetch_logs(
        pipeline_id: str,
        tail_lines: int = 200,
        component: str = "",
    ) -> str:
        """拉日志尾部（只读）。component 可填 comm/rat/bbh/bbl/marp/compare；留空为合并时间线。"""
        try:
            text = log_tool.fetch_logs(
                pipeline_id,
                tail_lines=tail_lines,
                component=component or None,
            )
            return _out(text)
        except Exception as exc:  # noqa: BLE001
            if durable:
                raise
            return _out(f"[fetch_logs error] {exc}")

    @tool
    def grep_logs(
        pipeline_id: str,
        pattern: str,
        context_lines: int = 3,
        max_matches: int = 20,
        component: str = "",
    ) -> str:
        """按正则检索日志全文。component 可限定单组件；留空同时检索六个组件。"""
        try:
            text = log_tool.grep_logs(
                pipeline_id,
                pattern,
                context_lines=context_lines,
                max_matches=max_matches,
                component=component or None,
            )
            return _out(text)
        except Exception as exc:  # noqa: BLE001
            if durable:
                raise
            return _out(f"[grep_logs error] {exc}")

    @tool
    def lookup_error_code(code: str) -> str:
        """按 errorcode 或摘要关键词查询可能故障组件、是否根因码和建议检查项。"""
        try:
            return _out(log_tool.lookup_error_code(code))
        except Exception as exc:  # noqa: BLE001
            if durable:
                raise
            return _out(f"[lookup_error_code error] {exc}")

    @tool
    def find_case_history(case_name: str, limit: int = 5) -> str:
        """查该用例近期流水线台账（只读），用于区分偶发失败 vs 持续失败。"""
        try:
            # 空串按台账回填后的语义走：过滤到"这个身份"，回填后没有行会匹配，
            # 不是退回不过滤——跟 pipeline_resolve.py 的处理方式保持一致。
            rows = ledger.find_by_case(case_name, limit=limit, user_id=user_id)
            payload: list[dict[str, Any]] = [
                {
                    "pipeline_id": r.pipeline_id,
                    "status": r.status,
                    "version": r.version,
                    "env": r.env,
                    "case_names": r.case_names,
                    "created_at": r.created_at,
                }
                for r in rows
            ]
            if not payload:
                return _out(f"[find_case_history] no records for case={case_name!r}")
            return _out(json.dumps(payload, ensure_ascii=False, indent=2))
        except Exception as exc:  # noqa: BLE001
            if durable:
                raise
            return _out(f"[find_case_history error] {exc}")

    @tool
    def get_case_spec(case_name: str) -> str:
        """获取用例元信息（名称/标题/标签），只读。"""
        try:
            cases = case_provider.fetch_cases([case_name])
            payload = [asdict(c) for c in cases]
            return _out(json.dumps(payload, ensure_ascii=False, indent=2))
        except Exception as exc:  # noqa: BLE001
            if durable:
                raise
            return _out(f"[get_case_spec error] {exc}")

    @tool
    def search_knowledge(query: str, top_k: int = 3) -> str:
        """检索故障知识/手册（旁证，只读）。主证据仍必须来自日志；不可用检索结果编造日志原文。"""
        try:
            text = knowledge.search(query, top_k=top_k)
            return _out(text)
        except Exception as exc:  # noqa: BLE001
            if durable:
                raise
            return _out(f"[search_knowledge error] {exc}")

    @tool
    def fetch_archived_block(artifact_id: str) -> str:
        """按 artifact 引用回读已归档内容（只读）。返回有界摘录，原文仍在 archive。"""
        try:
            from work_agent.graph.helpers.diagnosis_context import excerpt_archived_block

            return _out(excerpt_archived_block(archive.read(artifact_id)))
        except Exception as exc:  # noqa: BLE001
            if durable:
                raise
            return _out(f"[fetch_archived_block error] {exc}")

    tools = [
        get_pipeline_status,
        list_log_files,
        fetch_logs,
        grep_logs,
        lookup_error_code,
        find_case_history,
        get_case_spec,
        search_knowledge,
    ]
    if archive is not None:
        tools.append(fetch_archived_block)
    # 构造期自检：误把写操作工具混进这份只读白名单时直接拦住，不用等运行时才发现。
    assert_read_only_whitelist([t.name for t in tools])
    return tools


# 必须锁定流水线的工具。component 锁定只作用于拉日志/检索日志。
_PIPELINE_LOCKED_TOOLS = frozenset(
    {"get_pipeline_status", "list_log_files", "fetch_logs", "grep_logs"}
)
_COMPONENT_LOCKED_TOOLS = frozenset({"fetch_logs", "grep_logs"})


def apply_tool_scope(
    tool_name: str,
    args: dict[str, Any] | None,
    *,
    pipeline_id: str,
    component: str,
) -> dict[str, Any]:
    """
    校验并回填范围锁定参数。越界直接抛 ValueError，不靠 prompt。

    空的 pipeline_id / component 会被强制填成锁定值；填了其它值则拒绝。
    """
    locked_pipeline = (pipeline_id or "").strip()
    locked_component = (component or "").strip()
    if not locked_pipeline or not locked_component:
        raise ValueError("范围锁定需要非空的 pipeline_id 与 component")
    payload = dict(args or {})
    if tool_name in _PIPELINE_LOCKED_TOOLS:
        given = str(payload.get("pipeline_id") or "").strip()
        if given and given != locked_pipeline:
            raise ValueError(
                f"越界 pipeline_id={given!r}，本调查只允许 {locked_pipeline!r}"
            )
        payload["pipeline_id"] = locked_pipeline
    if tool_name in _COMPONENT_LOCKED_TOOLS:
        given_c = str(payload.get("component") or "").strip()
        if given_c and given_c != locked_component:
            raise ValueError(
                f"越界 component={given_c!r}，本调查只允许 {locked_component!r}"
            )
        payload["component"] = locked_component
    return payload


def scope_lock_tools(
    tools: Sequence[BaseTool],
    *,
    pipeline_id: str,
    component: str,
    allowed_names: Sequence[str] | None = None,
) -> list[BaseTool]:
    """
    包装诊断工具：强制 pipeline_id 与 component，并按白名单裁剪。

    参数:
        tools: ``build_diagnose_tools`` 的产物。
        pipeline_id / component: 本调查锁定范围。
        allowed_names: 组件 Registry 白名单；None 表示保留全部传入工具。
    """
    allow = None if allowed_names is None else {name for name in allowed_names}
    wrapped: list[BaseTool] = []
    for inner in tools:
        if allow is not None and inner.name not in allow:
            continue
        wrapped.append(_wrap_scoped_tool(inner, pipeline_id=pipeline_id, component=component))
    assert_read_only_whitelist([t.name for t in wrapped])
    return wrapped


def build_scoped_diagnose_tools(
    *,
    pipeline_id: str,
    component: str,
    allowed_names: Sequence[str] | None = None,
    **kwargs: Any,
) -> list[BaseTool]:
    """构造只读诊断工具并套上范围锁。其余参数原样传给 ``build_diagnose_tools``。"""
    return scope_lock_tools(
        build_diagnose_tools(**kwargs),
        pipeline_id=pipeline_id,
        component=component,
        allowed_names=allowed_names,
    )


def _wrap_scoped_tool(
    inner: BaseTool, *, pipeline_id: str, component: str
) -> BaseTool:
    """用同一 schema 包一层，运行时强制范围。"""

    def _run(**kwargs: Any) -> str:
        locked = apply_tool_scope(
            inner.name, kwargs, pipeline_id=pipeline_id, component=component
        )
        result = inner.invoke(locked)
        return result if isinstance(result, str) else str(result)

    schema = getattr(inner, "args_schema", None)
    tool = StructuredTool.from_function(
        func=_run,
        name=inner.name,
        description=inner.description,
        args_schema=schema,
    )
    return tool
