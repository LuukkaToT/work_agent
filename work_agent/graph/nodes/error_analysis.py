"""
error_analysis：受限 ReAct 归因（只读多工具）。

create/start 不在白名单；知识检索为旁证。
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.prebuilt import create_react_agent
from pydantic import BaseModel, Field

from work_agent.core.config import get_settings
from work_agent.core.llm import get_chat_model
from work_agent.core.skills import load_skill
from work_agent.graph.helpers.diagnose_tools import build_diagnose_tools
from work_agent.graph.helpers.truncate import CharBudget


class ErrorAnalysisOut(BaseModel):
    """结构化归因结果（由第二次 LLM 从正文抽取）。"""

    fail_kind: str = Field(description="version | case | env | unknown | none")
    evidence: str = Field(description="从日志摘录的关键证据，原文短摘")
    conclusion: str = Field(description="一句话结论")
    suggestion: str = Field(description="下一步建议")


def extract_tool_trace(messages: list) -> list[dict[str, Any]]:
    """
    从 ReAct 消息里提取 tool 调用轨迹（纯函数，可单测）。

    参数:
        messages: ReAct agent 返回的消息列表。

    返回:
        交替记录 call / result 的 dict 列表（含 name、预览或字符数）。
    """
    trace: list[dict[str, Any]] = []
    for msg in messages or []:
        if isinstance(msg, AIMessage):
            for tc in getattr(msg, "tool_calls", None) or []:
                if isinstance(tc, dict):
                    name = tc.get("name") or ""
                    args = tc.get("args") or {}
                else:
                    name = getattr(tc, "name", "") or ""
                    args = getattr(tc, "args", {}) or {}
                preview = json.dumps(args, ensure_ascii=False)
                if len(preview) > 200:
                    preview = preview[:200] + "..."
                trace.append({"type": "call", "name": name, "args_preview": preview})
        elif isinstance(msg, ToolMessage):
            content = getattr(msg, "content", "") or ""
            if not isinstance(content, str):
                content = str(content)
            trace.append(
                {
                    "type": "result",
                    "name": getattr(msg, "name", "") or "",
                    "content_chars": len(content),
                }
            )
    return trace


def _last_text(messages: list) -> str:
    """取消息列表中最后一条非空文本内容。"""
    for msg in reversed(messages or []):
        content = getattr(msg, "content", None)
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            parts = [
                str(b.get("text", "")) if isinstance(b, dict) else str(b)
                for b in content
            ]
            text = "".join(parts).strip()
            if text:
                return text
    return ""


def error_analysis(state: Mapping[str, Any]) -> dict:
    """
    对已消解的 pipelines 做受限 ReAct 归因（只读工具）。

    参数:
        state: 读 ``pipelines`` / ``user_input``。

    返回:
        ``summary``（含 error_analysis 结构化字段与 analysis_text）及 audit（含 tool_trace）。
    """
    pipelines = list(state.get("pipelines") or [])
    pids = [str(p.get("pipeline_id") or "") for p in pipelines if p.get("pipeline_id")]
    if not pids:
        return {
            "summary": {
                "status": "not_found",
                "message": "没有可诊断的流水线",
            },
            "audit": [{"step": "error_analysis", "status": "empty"}],
        }

    profile = get_settings().profile
    react_limit = profile.react_max_steps
    budget = CharBudget(limit=profile.react_total_chars_budget)

    try:
        pack = load_skill("error_analysis")
        # 知识走 search_knowledge tool，不把 kb 全量塞进 system
        system = pack.as_system_prompt(references={})
    except FileNotFoundError:
        system = (
            "你是测试失败归因助手。只用只读工具取证。"
            "禁止 create/start。不得编造未读到的日志。"
            "最后用中文给出 fail_kind、证据、结论、建议。"
        )

    tools = build_diagnose_tools(
        scenario="case_error",
        budget=budget,
        tool_result_max_chars=profile.tool_result_max_chars,
    )
    model = get_chat_model(temperature=0)
    agent = create_react_agent(model, tools, prompt=system)

    user_input = state.get("user_input") or ""
    brief = {
        "pipeline_ids": pids,
        "pipelines": [
            {
                "pipeline_id": p.get("pipeline_id"),
                "case_names": p.get("case_names"),
                "version": p.get("version"),
                "env": p.get("env"),
                "status": p.get("status"),
            }
            for p in pipelines
        ],
        "user_request": user_input,
    }
    human = (
        "请诊断下列流水线失败/异常原因。"
        "建议顺序：get_pipeline_status → fetch_logs / grep_logs → "
        "必要时 find_case_history / search_knowledge → 下结论。"
        "禁止任何写操作。\n"
        + json.dumps(brief, ensure_ascii=False, indent=2)
    )

    tool_trace: list[dict[str, Any]] = []
    try:
        result = agent.invoke(
            {"messages": [HumanMessage(content=human)]},
            config={"recursion_limit": react_limit},
        )
        messages = result.get("messages") or []
        tool_trace = extract_tool_trace(messages)
        analysis_text = _last_text(messages) or "未能生成归因结论"
    except Exception as exc:  # noqa: BLE001
        analysis_text = f"归因过程失败: {exc}"

    structured: dict[str, Any]
    try:
        structured_llm = get_chat_model(temperature=0).with_structured_output(
            ErrorAnalysisOut
        )
        parsed: ErrorAnalysisOut = structured_llm.invoke(
            [
                SystemMessage(
                    content="根据归因正文提取结构化字段；没有的信息填 unknown/空。"
                ),
                HumanMessage(content=analysis_text),
            ]
        )
        structured = parsed.model_dump()
    except Exception:  # noqa: BLE001
        structured = {
            "fail_kind": "unknown",
            "evidence": "",
            "conclusion": analysis_text[:200],
            "suggestion": "请人工查看流水线日志",
        }

    message = (
        f"归因结论：{structured.get('conclusion') or ''}；"
        f"类型={structured.get('fail_kind')}。"
        f"建议：{structured.get('suggestion') or ''}"
    )
    return {
        "summary": {
            "status": "ok",
            "message": message,
            "error_analysis": structured,
            "analysis_text": analysis_text,
        },
        "audit": [
            {
                "step": "error_analysis",
                "pipeline_ids": pids,
                "fail_kind": structured.get("fail_kind"),
                "react_limit": react_limit,
                "tool_trace": tool_trace,
                "budget_used": budget.used,
            }
        ],
    }
