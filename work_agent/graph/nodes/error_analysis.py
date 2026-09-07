"""
error_analysis：受限 ReAct 归因（只读多工具）+ 上下文管理。

create/start 不在白名单；知识检索为旁证。
主图只回收 summary/evidence/ruled_out，不把原始日志写入 messages。

节点本身只是薄壳，真正的流程在 ``run_diagnosis``：把它抽出来是为了让离线
A/B eval 能对同一个 case 跑两种上下文策略（legacy / managed），
而不用起图、也不用复制一份逻辑。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Sequence

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from pydantic import BaseModel, Field

from work_agent.core.config import get_settings
from work_agent.core.llm import get_fast_model, get_reasoning_model
from work_agent.core.skills import load_skill
from work_agent.core.usage import TokenUsage, usage_from_message
from work_agent.graph.helpers.agent_loop import run_agent_loop
from work_agent.graph.helpers.context_archive import ContextArchive
from work_agent.graph.helpers.context_budget import (
    PRIORITY_CONCLUSION,
    PRIORITY_EVIDENCE,
    PRIORITY_RAW_TOOL,
    PRIORITY_RULED_OUT,
    ContextBlock,
    assemble_blocks,
    compress_observation,
)
from work_agent.graph.helpers.context_compressor import ContextCompressor
from work_agent.graph.helpers.context_manager import ContextManager
from work_agent.graph.helpers.context_selector import (
    PIN_IMMUTABLE,
    PIN_NORMAL,
    PIN_PROTECTED,
    ContextItem,
)
from work_agent.graph.helpers.diagnose_tools import build_diagnose_tools
from work_agent.graph.helpers.truncate import CharBudget, clip_text

ContextStrategy = Literal["legacy", "managed"]

# goal 块（用户诉求）标为 immutable，所以必须自己有界
_GOAL_MAX_CHARS = 2000

_EXTRACT_SYSTEM = (
    "根据下列压缩上下文提取结构化字段。"
    "root_component 填最可能的首个故障组件，而不是最后报级联错误的组件；"
    "evidence 必须来自工具摘录原文短摘；"
    "ruled_out 只保留已排除假设及一句话理由；"
    "没有的信息填 unknown/空列表。"
)


class RuledOutItem(BaseModel):
    """已排除假设的一条记录。"""

    hypothesis: str = Field(description="已排除的假设")
    reason: str = Field(description="排除理由（一句话）")


class ErrorAnalysisOut(BaseModel):
    """结构化归因结果（由第二次 LLM 从正文抽取）。"""

    fail_kind: str = Field(description="version | case | env | unknown | none")
    root_component: str = Field(
        default="unknown",
        description="最可能根因组件，如 comm/rat/bbh/bbl/marp/compare/none/unknown"
    )
    root_cause: str = Field(default="unknown", description="一句话根因；证据不足时填 unknown")
    evidence: str = Field(description="从日志摘录的关键证据，原文短摘")
    conclusion: str = Field(description="一句话结论")
    suggestion: str = Field(description="下一步建议")
    ruled_out: list[RuledOutItem] = Field(
        default_factory=list,
        description="已排除的假设（只保留结论，不保留排查全过程）",
    )


@dataclass(frozen=True)
class DiagnosisResult:
    """一次归因的产出与代价（eval 直接消费这些字段）。"""

    structured: dict[str, Any]
    analysis_text: str
    message: str
    strategy: str
    # 真正喂给结构化抽取的 working context 原文。eval 的 evidence_recall 在它上面
    # 算：证据「有没有活到最后一步」才是上下文管理的成败，不是它曾经出现过。
    context_text: str = ""
    selected_context_ids: list[str] = field(default_factory=list)
    context_chars: int = 0
    latency_ms: int = 0
    token_usage: dict[str, int] = field(default_factory=dict)
    tool_calls: int = 0
    tool_trace: list[dict[str, Any]] = field(default_factory=list)
    budget_used: int = 0
    obs_compressed_n: int = 0
    ruled_out_n: int = 0
    react_limit: int = 0
    trimmed_steps: int = 0
    compressed_ids: list[str] = field(default_factory=list)
    # 最后一次真正发给 ReAct 模型的历史字符数（含 prelude）。和抽取
    # context_chars 不是同一层：legacy 不裁历史，managed 受 react_history_max_chars 约束。
    react_context_chars: int = 0
    # 归档条数（managed 且传了 archive 时 > 0）：被裁历史 + 抽取期被压块的原文都在里面。
    archived_n: int = 0

    @property
    def fail_kind(self) -> str:
        """归因类型（version / case / env / unknown / none）。"""
        return str(self.structured.get("fail_kind") or "unknown")

    @property
    def root_component(self) -> str:
        """最可能的首个故障组件。"""
        return str(self.structured.get("root_component") or "unknown")


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
    for name, text in _iter_observations(messages):
        compressed = compress_observation(text, max_chars=max_chars_each)
        if compressed.strip():
            out.append(f"{name}: {compressed}")
    return out


def collect_observation_items(messages: list) -> list[ContextItem]:
    """
    把每条 ToolMessage 变成一个独立 ContextItem（managed 策略用）。

    不预先压缩：压不压、压多狠由 ContextManager 按预算决定，
    这里提前截断会让它失去判断依据。

    参数:
        messages: ReAct 消息列表。

    返回:
        ContextItem 列表，pin 为 normal、优先级为原始工具输出。
    """
    items: list[ContextItem] = []
    for idx, (name, text) in enumerate(_iter_observations(messages)):
        if not text.strip():
            continue
        items.append(
            ContextItem(
                item_id=f"obs-{idx + 1:03d}-{name}",
                kind="tool_result",
                source=name,
                text=text,
                priority=PRIORITY_RAW_TOOL,
                pin=PIN_NORMAL,
            )
        )
    return items


def run_diagnosis(
    *,
    pipelines: Sequence[Mapping[str, Any]],
    user_input: str = "",
    user_id: str = "",
    context_strategy: ContextStrategy = "managed",
    scenario: str = "case_error",
    run_id: str = "",
    compressor: ContextCompressor | None = None,
    archive: ContextArchive | None = None,
) -> DiagnosisResult:
    """
    对已消解的 pipelines 跑一次受限 ReAct 归因。

    两种上下文策略：

    - ``legacy``：ReAct 历史不裁剪，抽取上下文用 ``assemble_blocks`` 直接按
      优先级丢块。即改造前的行为，留着做 A/B 基线。
    - ``managed``：ReAct 历史按 step 裁剪，抽取上下文走 ContextManager
      （dedup / 相关性排序 / 超预算才压缩 / 原文归档）。

    参数:
        pipelines: 已消解的流水线 brief 列表。
        user_input: 本轮用户诉求，同时作为相关性打分的 goal。
        user_id: 工号，``find_case_history`` 按它过滤。
        context_strategy: legacy 或 managed。
        scenario: mock 故障场景（真实后端下无意义）。
        run_id: 归档目录名，一般传 task_id。
        compressor: 注入用（单测 / eval）；None 用默认快模型压缩器。
        archive: 注入用；None 时 managed 策略自建一个按 run_id 分目录的归档。

    返回:
        DiagnosisResult。
    """
    started = time.perf_counter()
    profile = get_settings().profile
    react_limit = profile.react_max_steps
    budget = CharBudget(limit=profile.react_total_chars_budget)
    managed = context_strategy == "managed"
    usage = TokenUsage()

    # 归档器在 ReAct 循环前就建好（仅 managed）：循环内裁剪历史时当场落盘，
    # 抽取期 ContextManager 复用同一实例，两阶段共享一份 external context。
    # legacy 恒为 None，基线行为不变。
    live_archive: ContextArchive | None = None
    if managed:
        live_archive = archive if archive is not None else ContextArchive(run_id=run_id)

    system = _load_system_prompt()
    tools = build_diagnose_tools(
        scenario=scenario,  # type: ignore[arg-type]
        budget=budget,
        tool_result_max_chars=profile.tool_result_max_chars,
        user_id=user_id,
        archive=live_archive,
    )
    model = get_reasoning_model(temperature=0)  # Role：开放式多步推理
    human = _build_human_prompt(pipelines, user_input)

    tool_trace: list[dict[str, Any]] = []
    obs_compressed: list[str] = []
    obs_items: list[ContextItem] = []
    trimmed_steps = 0
    react_context_chars = 0
    analysis_text = ""
    try:
        loop = run_agent_loop(
            model=model,
            tools=tools,
            system=system,
            user=human,
            max_steps=react_limit,
            observation_max_chars=profile.react_observation_max_chars,
            history_max_chars=profile.react_history_max_chars if managed else 0,
            archive=live_archive,
        )
        usage = usage + loop.usage
        trimmed_steps = loop.trimmed_steps
        react_context_chars = loop.context_chars
        tool_trace = extract_tool_trace(loop.messages)
        obs_compressed = collect_compressed_observations(loop.messages)
        obs_items = collect_observation_items(loop.messages)
        analysis_text = _last_text(loop.messages) or "未能生成归因结论"
    except Exception as exc:  # noqa: BLE001
        analysis_text = f"归因过程失败: {exc}"

    limit = min(12_000, profile.react_total_chars_budget // 2)
    selected_ids: list[str] = []
    compressed_ids: list[str] = []
    if managed:
        manager = ContextManager(compressor=compressor, archive=live_archive)
        rendered = manager.render(
            _managed_items(user_input, analysis_text, obs_items),
            goal=user_input or analysis_text,
            limit=limit,
        )
        extract_ctx = rendered.text
        selected_ids = rendered.selected_ids
        compressed_ids = rendered.compressed_ids
        context_chars = rendered.context_chars
        # 压缩自身烧掉的 token 必须计入总量，否则 A/B 是自欺
        usage = usage + rendered.usage
    else:
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
            limit=limit,
        )
        context_chars = len(extract_ctx)

    structured, extract_usage = _extract_structured(
        extract_ctx, analysis_text=analysis_text, obs_compressed=obs_compressed
    )
    usage = usage + extract_usage

    long_term, message = diagnosis_conclusion(structured, analysis_text)
    ruled = structured.get("ruled_out") or []
    return DiagnosisResult(
        structured=structured,
        analysis_text=long_term,
        message=message,
        strategy=context_strategy,
        context_text=extract_ctx,
        selected_context_ids=selected_ids,
        context_chars=context_chars,
        latency_ms=int((time.perf_counter() - started) * 1000),
        token_usage=usage.as_dict(),
        tool_calls=sum(1 for t in tool_trace if t.get("type") == "call"),
        tool_trace=tool_trace,
        budget_used=budget.used,
        obs_compressed_n=len(obs_compressed),
        ruled_out_n=len(ruled),
        react_limit=react_limit,
        trimmed_steps=trimmed_steps,
        compressed_ids=compressed_ids,
        react_context_chars=react_context_chars,
        archived_n=len(live_archive.refs) if live_archive is not None else 0,
    )


def diagnosis_conclusion(structured: dict, analysis_text: str) -> tuple[str, str]:
    """Shared online/offline presentation, without model or storage work."""
    ruled = structured.get("ruled_out") or []
    long_term = assemble_blocks(
        [
            ContextBlock(
                "conclusion", str(structured.get("conclusion") or ""), PRIORITY_CONCLUSION
            ),
            ContextBlock(
                "evidence", str(structured.get("evidence") or ""), PRIORITY_EVIDENCE
            ),
            ContextBlock(
                "ruled_out",
                json.dumps(ruled, ensure_ascii=False) if ruled else "",
                PRIORITY_RULED_OUT,
            ),
        ],
        limit=4000,
    )

    message = (
        f"归因结论：{structured.get('conclusion') or ''}；"
        f"类型={structured.get('fail_kind')}，"
        f"根因组件={structured.get('root_component') or 'unknown'}。"
        f"建议：{structured.get('suggestion') or ''}"
    )
    return long_term or analysis_text[:1500], message


def error_analysis(state: Mapping[str, Any]) -> dict:
    """
    对已消解的 pipelines 做受限 ReAct 归因（只读工具）。

    回写主图：summary（含 evidence / ruled_out），不含原始日志全文。

    参数:
        state: 读 ``pipelines`` / ``user_input`` / ``user_id`` / ``task_id``。

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

    result = run_diagnosis(
        pipelines=pipelines,
        user_input=str(state.get("user_input") or ""),
        user_id=str(state.get("user_id") or ""),
        context_strategy="managed",
        run_id=str(state.get("task_id") or ""),
    )
    return diagnosis_output(result, pids)


def diagnosis_output(result: DiagnosisResult, pids: list[str]) -> dict:
    """Expose only compact output and audit to the enclosing graph."""
    return {
        "summary": {
            "status": "ok",
            "message": result.message,
            "error_analysis": result.structured,
            # analysis_text 用优先级裁剪后的长期上下文，避免整段 ReAct 原文常驻
            "analysis_text": result.analysis_text,
        },
        "audit": [
            {
                "step": "error_analysis",
                "pipeline_ids": pids,
                "fail_kind": result.fail_kind,
                "react_limit": result.react_limit,
                "tool_trace": result.tool_trace,
                "budget_used": result.budget_used,
                "obs_compressed_n": result.obs_compressed_n,
                "ruled_out_n": result.ruled_out_n,
                "context_strategy": result.strategy,
                "context_chars": result.context_chars,
                "selected_context_ids": result.selected_context_ids,
                "trimmed_steps": result.trimmed_steps,
                "latency_ms": result.latency_ms,
                "token_usage": result.token_usage,
            }
        ],
    }


def _managed_items(
    user_input: str,
    analysis_text: str,
    obs_items: list[ContextItem],
) -> list[ContextItem]:
    """
    组装 managed 策略的候选上下文块，并标好 pin 等级。

    goal 先夹到 ``_GOAL_MAX_CHARS`` 再标 immutable：immutable 超预算会直接抛
    ``ImmutableBudgetExceeded``，那个守卫是留给「prompt/预算配置写错」的，
    不该让一段超长用户输入变成崩溃入口。
    """
    items: list[ContextItem] = []
    goal = clip_text((user_input or "").strip(), max_chars=_GOAL_MAX_CHARS)
    if goal:
        # 用户诉求不能被裁掉，否则抽取器不知道在回答什么问题
        items.append(
            ContextItem(
                item_id="goal",
                kind="note",
                source="user_request",
                text=goal,
                priority=PRIORITY_CONCLUSION,
                pin=PIN_IMMUTABLE,
            )
        )
    if analysis_text.strip():
        # 结论草稿不允许删除，但过长时可以压成摘要
        items.append(
            ContextItem(
                item_id="conclusion_draft",
                kind="conclusion",
                source="react",
                text=analysis_text,
                priority=PRIORITY_CONCLUSION,
                pin=PIN_PROTECTED,
            )
        )
    items.extend(obs_items)
    return items


def _extract_structured(
    extract_ctx: str,
    *,
    analysis_text: str,
    obs_compressed: list[str],
    strict: bool = False,
) -> tuple[dict[str, Any], TokenUsage]:
    """
    二次结构化抽取。

    用 ``include_raw=True`` 是为了拿到原始 AIMessage 里的 token 用量 ——
    否则这次调用的成本在 eval 里凭空消失。

    返回:
        ``(结构化字段, 本次调用的 token 用量)``；解析失败时给确定性降级结果。
    """
    try:
        # Flow：纯字段抽取，非推理，用快模型
        # method="function_calling"：OpenRouter 等网关对 json_schema/parse 透传不稳，
        # 会把 markdown 围栏原样塞进 content 或改写字段名；工具调用协议则各端点一致。
        structured_llm = get_fast_model(temperature=0).with_structured_output(
            ErrorAnalysisOut, include_raw=True, method="function_calling"
        )
        raw = structured_llm.invoke(
            [
                SystemMessage(content=_EXTRACT_SYSTEM),
                HumanMessage(content=extract_ctx),
            ]
        )
        if isinstance(raw, dict):
            usage = usage_from_message(raw.get("raw"))
            parsed = raw.get("parsed")
        else:
            usage = TokenUsage()
            parsed = raw
        if parsed is None:
            raise ValueError("结构化抽取未返回 parsed")
        return parsed.model_dump(), usage
    except Exception:  # noqa: BLE001
        if strict:
            raise
        return {
            "fail_kind": "unknown",
            "root_component": "unknown",
            "root_cause": "unknown",
            "evidence": "\n".join(obs_compressed)[:500],
            "conclusion": analysis_text[:200],
            "suggestion": "请人工查看流水线日志",
            "ruled_out": [],
        }, TokenUsage()


def _load_system_prompt() -> str:
    """加载 error_analysis skill；缺文件时用内置兜底 prompt。"""
    try:
        pack = load_skill("error_analysis")
        return pack.as_system_prompt(references={})
    except FileNotFoundError:
        return (
            "你是测试失败归因助手。只用只读工具取证。"
            "禁止 create/start。不得编造未读到的日志。"
            "最后用中文给出 fail_kind、root_component、root_cause、证据、结论、建议、ruled_out。"
        )


def _build_human_prompt(
    pipelines: Sequence[Mapping[str, Any]], user_input: str
) -> str:
    """拼首轮 human 输入：诊断顺序建议 + 已消解的流水线 brief。"""
    brief = {
        "pipeline_ids": [
            str(p.get("pipeline_id") or "") for p in pipelines if p.get("pipeline_id")
        ],
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
    return (
        "请诊断下列流水线失败/异常原因。"
        "建议顺序：get_pipeline_status → fetch_logs / grep_logs → "
        "必要时 find_case_history / search_knowledge → 下结论。"
        "排除过的假设写入 ruled_out（只留假设+理由）。禁止任何写操作。\n"
        + json.dumps(brief, ensure_ascii=False, indent=2)
    )


def _iter_observations(messages: list):
    """遍历 ToolMessage，产出 ``(工具名, 原文)``。"""
    for msg in messages or []:
        if not isinstance(msg, ToolMessage):
            continue
        content = getattr(msg, "content", "") or ""
        if not isinstance(content, str):
            content = str(content)
        yield (getattr(msg, "name", "") or "tool"), content


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
