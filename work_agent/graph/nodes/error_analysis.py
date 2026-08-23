"""
error_analysis：受限 ReAct 归因（只读多工具）+ 上下文压缩。

create/start 不在白名单；知识检索为旁证。
主图只回收 summary/evidence/ruled_out，不把原始日志写入 messages。
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from pydantic import BaseModel, Field

from work_agent.core.config import get_settings
from work_agent.core.llm import get_chat_model
from work_agent.core.skills import load_skill
from work_agent.graph.helpers.agent_loop import run_agent_loop
from work_agent.graph.helpers.context_budget import (
    PRIORITY_CONCLUSION,
    PRIORITY_EVIDENCE,
    PRIORITY_RAW_TOOL,
    PRIORITY_RULED_OUT,
    ContextBlock,
    assemble_blocks,
    compress_observation,
)
from work_agent.graph.helpers.diagnose_tools import build_diagnose_tools
from work_agent.graph.helpers.truncate import CharBudget


class RuledOutItem(BaseModel):
    """已排除假设的一条记录。"""

    hypothesis: str = Field(description="已排除的假设")
    reason: str = Field(description="排除理由（一句话）")


class ErrorAnalysisOut(BaseModel):
    """结构化归因结果（由第二次 LLM 从正文抽取）。"""

    fail_kind: str = Field(description="version | case | env | unknown | none")
    evidence: str = Field(description="从日志摘录的关键证据，原文短摘")
    conclusion: str = Field(description="一句话结论")
    suggestion: str = Field(description="下一步建议")
    ruled_out: list[RuledOutItem] = Field(
        default_factory=list,
        description="已排除的假设（只保留结论，不保留排查全过程）",
    )


def extract_tool_trace(messages: list) -> list[dict[str, Any]]:
    """从 ReAct 消息里提取 tool 调用轨迹（纯函数，可单测）。"""
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


def collect_compressed_observations(
    messages: list,
    *,
    max_chars_each: int = 400,
) -> list[str]:
    """
    把 ToolMessage 压成短 evidence 摘录列表。

    参数:
        messages: ReAct 消息列表。
        max_chars_each: 每条 observation 压缩后的字符上限。

    返回:
        ``工具名: 压缩摘录`` 字符串列表。
    """
    out: list[str] = []
    for msg in messages or []:
        if not isinstance(msg, ToolMessage):
            continue
        content = getattr(msg, "content", "") or ""
        if not isinstance(content, str):
            content = str(content)
        name = getattr(msg, "name", "") or "tool"
        compressed = compress_observation(content, max_chars=max_chars_each)
        if compressed.strip():
            out.append(f"{name}: {compressed}")
    return out


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

    回写主图：summary（含 evidence / ruled_out），不含原始日志全文。

    参数:
        state: 读 ``pipelines`` / ``user_input``。

    返回:
        ``summary``（error_analysis 结构化字段、裁剪后的 analysis_text）及 audit。
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
        system = pack.as_system_prompt(references={})
    except FileNotFoundError:
        system = (
            "你是测试失败归因助手。只用只读工具取证。"
            "禁止 create/start。不得编造未读到的日志。"
            "最后用中文给出 fail_kind、证据、结论、建议、ruled_out。"
        )

    tools = build_diagnose_tools(
        scenario="case_error",
        budget=budget,
        tool_result_max_chars=profile.tool_result_max_chars,
    )
    model = get_chat_model(temperature=0)

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
        "排除过的假设写入 ruled_out（只留假设+理由）。禁止任何写操作。\n"
        + json.dumps(brief, ensure_ascii=False, indent=2)
    )

    tool_trace: list[dict[str, Any]] = []
    obs_compressed: list[str] = []
    try:
        messages = run_agent_loop(
            model=model,
            tools=tools,
            system=system,
            user=human,
            max_steps=react_limit,
        )
        tool_trace = extract_tool_trace(messages)
        obs_compressed = collect_compressed_observations(messages)
        analysis_text = _last_text(messages) or "未能生成归因结论"
    except Exception as exc:  # noqa: BLE001
        analysis_text = f"归因过程失败: {exc}"

    # 按优先级组装给二次结构化抽取的上下文（超预算砍低优先级）
    extract_ctx = assemble_blocks(
        [
            ContextBlock("conclusion_draft", analysis_text, PRIORITY_CONCLUSION),
            ContextBlock(
                "evidence_from_tools",
                "\n---\n".join(obs_compressed),
                PRIORITY_EVIDENCE,
            ),
            ContextBlock(
                "raw_tool_note",
                "（原始 tool 全文未进入主会话；仅保留压缩摘录）",
                PRIORITY_RAW_TOOL,
            ),
        ],
        limit=min(12_000, profile.react_total_chars_budget // 2),
    )

    structured: dict[str, Any]
    try:
        structured_llm = get_chat_model(temperature=0).with_structured_output(
            ErrorAnalysisOut
        )
        parsed: ErrorAnalysisOut = structured_llm.invoke(
            [
                SystemMessage(
                    content=(
                        "根据下列压缩上下文提取结构化字段。"
                        "evidence 必须来自工具摘录原文短摘；"
                        "ruled_out 只保留已排除假设及一句话理由；"
                        "没有的信息填 unknown/空列表。"
                    )
                ),
                HumanMessage(content=extract_ctx),
            ]
        )
        structured = parsed.model_dump()
    except Exception:  # noqa: BLE001
        structured = {
            "fail_kind": "unknown",
            "evidence": "\n".join(obs_compressed)[:500],
            "conclusion": analysis_text[:200],
            "suggestion": "请人工查看流水线日志",
            "ruled_out": [],
        }

    # 回写前再压一遍长期字段（主图不存原始日志）
    ruled = structured.get("ruled_out") or []
    ruled_text = json.dumps(ruled, ensure_ascii=False) if ruled else ""
    long_term = assemble_blocks(
        [
            ContextBlock(
                "conclusion",
                str(structured.get("conclusion") or ""),
                PRIORITY_CONCLUSION,
            ),
            ContextBlock(
                "evidence",
                str(structured.get("evidence") or ""),
                PRIORITY_EVIDENCE,
            ),
            ContextBlock("ruled_out", ruled_text, PRIORITY_RULED_OUT),
        ],
        limit=4000,
    )

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
            # analysis_text 用优先级裁剪后的长期上下文，避免整段 ReAct 原文常驻
            "analysis_text": long_term or analysis_text[:1500],
        },
        "audit": [
            {
                "step": "error_analysis",
                "pipeline_ids": pids,
                "fail_kind": structured.get("fail_kind"),
                "react_limit": react_limit,
                "tool_trace": tool_trace,
                "budget_used": budget.used,
                "obs_compressed_n": len(obs_compressed),
                "ruled_out_n": len(ruled),
            }
        ],
    }
