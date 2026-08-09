"""
error_analysis：受限 ReAct 归因框架。

本期：mock fetch_logs + skill 骨架 + 步数上限；不填知识库。
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent
from pydantic import BaseModel, Field

from work_agent.core.llm import get_chat_model
from work_agent.core.skills import load_skill
from work_agent.tools.registry import get_log_tool

_MAX_REACT_STEPS = 8


class ErrorAnalysisOut(BaseModel):
    fail_kind: str = Field(description="version | case | env | unknown | none")
    evidence: str = Field(description="从日志摘录的关键证据，原文短摘")
    conclusion: str = Field(description="一句话结论")
    suggestion: str = Field(description="下一步建议")


def _build_fetch_logs_tool():
    log_tool = get_log_tool()

    @tool
    def fetch_logs(pipeline_id: str) -> str:
        """按 pipeline_id 拉取执行日志（只读）。"""
        return log_tool.fetch_logs(pipeline_id)

    return fetch_logs


def error_analysis(state: Mapping[str, Any]) -> dict:
    """
    对已消解的 pipelines 做受限 ReAct 归因。
    禁止 create/start；仅白名单 fetch_logs。
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

    try:
        pack = load_skill("error_analysis")
        system = pack.as_system_prompt(references={})  # 本期不注入知识库
    except FileNotFoundError:
        system = (
            "你是测试失败归因助手。只能用 fetch_logs 工具读日志。"
            "禁止编造未读到的日志。最后用中文给出 fail_kind、证据、结论、建议。"
        )

    fetch_logs = _build_fetch_logs_tool()
    model = get_chat_model(temperature=0)
    agent = create_react_agent(model, [fetch_logs], prompt=system)

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
        "请诊断下列流水线失败/异常原因。先 fetch_logs，再下结论。"
        "不要调用其它写操作。\n"
        + json.dumps(brief, ensure_ascii=False, indent=2)
    )

    try:
        result = agent.invoke(
            {"messages": [HumanMessage(content=human)]},
            config={"recursion_limit": _MAX_REACT_STEPS},
        )
        messages = result.get("messages") or []
        last = ""
        for msg in reversed(messages):
            content = getattr(msg, "content", None)
            if isinstance(content, str) and content.strip():
                last = content.strip()
                break
            if isinstance(content, list):
                parts = [
                    str(b.get("text", "")) if isinstance(b, dict) else str(b)
                    for b in content
                ]
                text = "".join(parts).strip()
                if text:
                    last = text
                    break
        analysis_text = last or "未能生成归因结论"
    except Exception as exc:  # noqa: BLE001
        analysis_text = f"归因过程失败: {exc}"

    # 再压成结构化摘要（失败则兜底）
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
                "react_limit": _MAX_REACT_STEPS,
            }
        ],
    }
