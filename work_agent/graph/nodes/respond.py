"""
respond：所有分支的统一收尾节点。

在它之前，每个分支产出的是结构化 summary（给报告、台账、程序看）；
在它之后才有一句给人看的话，并追加到 messages，供下一轮指代。

两条设计约束：

1. 不能编造。pipeline_id、用例名、版本、组网、路径、数量一旦被模型改写，
   用户会拿着错信息去查问题。所以事实以 JSON 原样给出，prompt 里
   把「只能引用给定事实」写成硬约束。
2. 不能中断。它是所有分支的必经节点，LLM 抖一下不能让整轮任务失败，
   因此有 _fallback_reply 兜底。

任务类型只读 state["intent"]，不看 summary["branch"]（单一事实来源）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from work_agent.core.config import get_settings
from work_agent.core.llm import invoke_text_fast
from work_agent.graph.state import TestFlowState

_MAX_ANALYSIS_LINES = 40

_SYSTEM = """你是资深测试工程师的助手，负责把一次任务的执行事实，转述成同事之间说话的样子。

硬性要求：
1. 只能使用【事实】里出现的信息。用例名、版本号、环境 IP、pipeline_id、文件路径、数量必须原样引用，一个字符都不要改。
2. 【事实】里没有的东西不要提，也不要推测。用户问到而事实里没有的，直接说没有记录到。
3. 用中文，口语化，不要用 markdown 标题和加粗，罗列不要超过三条。
4. 总长度控制在 5 行以内。
5. 最后一句给一个具体的下一步建议（比如可以查什么、可以怎么继续），不要客套话。
6. 若是执行提交成功：提醒用户到流水线前端看最终结果，也可稍后问进度；若只创建未启动，提醒可以说启动。"""


def _facts(state: TestFlowState) -> dict:
    """把 state 收敛成一张事实卡片。引用性字段读顶层，汇总量读 summary。"""
    summary = state.get("summary") or {}
    params = state.get("exec_params") or {}
    pipelines = state.get("pipelines") or []
    facts: dict[str, Any] = {}

    def put(key: str, value: Any) -> None:
        if value is None:
            return
        if isinstance(value, (str, list, dict, tuple)) and len(value) == 0:
            return
        facts[key] = value

    put("用户问题", state.get("user_input"))
    put("任务类型", state.get("intent"))
    put("任务状态", summary.get("status"))
    if params.get("exec_mode"):
        put("执行模式", params.get("exec_mode"))

    plans = params.get("plans") or []
    if plans:
        put(
            "执行计划",
            [
                {
                    "用例": p.get("case_names"),
                    "版本": p.get("version"),
                    "环境": p.get("env"),
                }
                for p in plans
            ],
        )

    if pipelines:
        put(
            "流水线",
            [
                {
                    "pipeline_id": p.get("pipeline_id"),
                    "环境": p.get("env"),
                    "版本": p.get("version"),
                    "用例": p.get("case_names"),
                    "状态": p.get("status"),
                    "错误": p.get("error") or None,
                }
                for p in pipelines
            ],
        )
        put(
            "pipeline_id列表",
            [p.get("pipeline_id") for p in pipelines if p.get("pipeline_id")],
        )

    total = summary.get("total")
    if isinstance(total, int):
        put("用例总数", total)
        put("通过数", summary.get("passed") or 0)
        put("失败数", summary.get("failed_count", len(summary.get("failed") or [])))

    failed = summary.get("failed") or []
    if failed:
        put(
            "失败明细",
            [
                {
                    "用例": item.get("case_name"),
                    "结论": item.get("verdict"),
                    "归因": item.get("fail_kind"),
                    "详情": item.get("detail"),
                }
                for item in failed
            ],
        )

    if summary.get("created") is not None:
        put("已处理流水线数", summary.get("created"))
    if summary.get("failed_pipelines"):
        put("失败流水线数", summary.get("failed_pipelines"))

    put("分析文档", state.get("analysis_path"))
    put("系统备注", summary.get("message"))
    return facts


def _analysis_excerpt(path_str: str) -> str:
    """读取分析文档前若干行，供 respond 概括用。"""
    if not path_str:
        return ""
    path = Path(path_str)
    if not path.exists():
        return ""
    lines = path.read_text(encoding="utf-8").splitlines()
    excerpt = "\n".join(lines[:_MAX_ANALYSIS_LINES])
    if len(lines) > _MAX_ANALYSIS_LINES:
        excerpt += f"\n...（全文共 {len(lines)} 行，此处省略）"
    return excerpt


def _fallback_reply(state: TestFlowState, facts: dict) -> str:
    """LLM 不可用时的确定性回复：信息量比模型版少，但绝不会错。"""
    summary = state.get("summary") or {}
    intent = state.get("intent") or ""
    status = summary.get("status") or ""
    pipelines = state.get("pipelines") or []

    if intent == "analysis" and state.get("analysis_path"):
        return f"测试分析已生成，文档在 {state['analysis_path']}。"

    if status == "cancelled":
        return "已按你的意思取消，没有提交执行。"

    if intent == "execute" and pipelines:
        pids = [p.get("pipeline_id") for p in pipelines if p.get("pipeline_id")]
        n = summary.get("created", len(pids))
        mode = (state.get("exec_params") or {}).get("exec_mode") or summary.get(
            "exec_mode"
        )
        if status == "created" or mode == "create_only":
            text = (
                f"已创建 {n} 条流水线（{', '.join(pids)}），尚未启动。"
                "需要时可让我按 pipeline_id 启动。"
            )
        else:
            text = (
                f"已创建并启动 {n} 条流水线（{', '.join(pids)}），"
                "结果请到流水线前端查看，也可稍后问我进度。"
            )
        failed_n = summary.get("failed_pipelines") or 0
        if failed_n:
            text += f"另有 {failed_n} 条失败。"
        return text

    if intent == "start" and pipelines:
        pids = [p.get("pipeline_id") for p in pipelines if p.get("pipeline_id")]
        return str(
            summary.get("message")
            or f"已处理启动：{', '.join(pids)}"
        )

    if intent == "query":
        return str(summary.get("message") or "没查到匹配的执行记录。")

    return json.dumps(facts, ensure_ascii=False, indent=2)


def _persist_reply(task_id: str, reply: str) -> None:
    """
    把回复留痕，事后复盘「当时到底告诉了用户什么」。

    只在任务目录已存在时写，避免闲聊也造一堆空目录。
    """
    if not task_id or not reply:
        return
    run_dir = get_settings().workspace_dir / "runs" / task_id
    if not run_dir.exists():
        return
    try:
        (run_dir / "reply.md").write_text(reply + "\n", encoding="utf-8")
    except OSError:
        pass


def _pack(reply: str, *, source: str, error: str = "") -> dict:
    """统一出口：reply + 会话历史追加 AIMessage + audit。"""
    record: dict[str, Any] = {"step": "respond", "source": source, "chars": len(reply)}
    if error:
        record["error"] = error
    return {
        "reply": reply,
        "messages": [AIMessage(content=reply)],
        "audit": [record],
    }


def respond(state: TestFlowState) -> dict:
    """
    全部分支收尾：把结构化事实转成口语回复，追加到 messages。

    参数:
        state: 读 intent / summary / pipelines / exec_params / analysis_path 等。

    返回:
        ``reply``、追加的 AIMessage、以及 audit（source=llm/fallback/passthrough）。
    """
    summary = state.get("summary") or {}
    intent = state.get("intent") or ""
    task_id = state.get("task_id") or ""

    if intent == "chat" and summary.get("answer"):
        reply = str(summary["answer"]).strip()
        _persist_reply(task_id, reply)
        return _pack(reply, source="passthrough")

    facts = _facts(state)
    user_parts = ["【事实】", json.dumps(facts, ensure_ascii=False, indent=2)]

    if intent == "analysis":
        excerpt = _analysis_excerpt(state.get("analysis_path") or "")
        if excerpt:
            user_parts += [
                "",
                "【刚生成的分析文档节选，只用来概括它讲了哪几块，不要整篇复述】",
                excerpt,
            ]

    user_parts += ["", "请据此回答用户。"]

    error = ""
    try:
        reply = invoke_text_fast(
            [
                SystemMessage(content=_SYSTEM),
                HumanMessage(content="\n".join(user_parts)),
            ],
            temperature=0.3,
        )
        source = "llm"
    except Exception as exc:  # noqa: BLE001 - 必经节点，不能让它中断整轮任务
        reply = _fallback_reply(state, facts)
        source = "fallback"
        error = str(exc)

    if not reply.strip():
        reply = _fallback_reply(state, facts)
        source = "fallback"

    _persist_reply(task_id, reply)
    return _pack(reply, source=source, error=error)
