"""从用户话消解台账中的流水线记录（query / start 共用）。"""

from __future__ import annotations

import re

from langgraph.types import interrupt

from work_agent.core.ledger import PipelineRecord, get_ledger

# 标准 uuid，或历史 pipe-/local- 前缀
_PIPELINE_ID_RE = re.compile(
    r"\b("
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|pipe-[a-f0-9]+"
    r"|local-[a-f0-9]+"
    r")\b",
    re.I,
)
_CASE_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9_-]{7,})\b")
_LAST_RE = re.compile(r"(上次|前面|刚才|最近一次|上一次|那两?条|那几条)")
_ALL_RE = re.compile(r"(所有|全部|每一条|每一条流水线|当前所有)")
# 第 N 条 / 第一条 / 第1条 / 第一个
_CN_ORD = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}
_ORDINAL_RE = re.compile(
    r"(?:第\s*([一二两三四五六七八九十\d]+)\s*条"
    r"|第\s*([一二两三四五六七八九十\d]+)\s*个"
    r"|^\s*([一二两三四五六七八九十\d]+)\s*$)"
)


def _parse_ordinal(text: str) -> int | None:
    """把『第一条』/『1』解析成 1-based 序号；失败返回 None。"""
    m = _ORDINAL_RE.search(text or "")
    if not m:
        return None
    raw = next((g for g in m.groups() if g), None)
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    if raw in _CN_ORD:
        return _CN_ORD[raw]
    return None


def resolve_pipeline_records(
    user_input: str, *, user_id: str | None = None
) -> list[PipelineRecord]:
    """
    从用户话消解台账中的候选流水线（尚不 interrupt）。

    参数:
        user_input: 本轮用户输入（可含 pipeline_id / 用例名 /「上次」「全部」等）。
        user_id: 不为 None 时只在该用户名下的记录里消解；None 表示不过滤
            （当前无生产调用点这么用，只留给管理/调试场景）。

    返回:
        候选 PipelineRecord 列表；无法消解时可能返回最近若干条。
    """
    ledger = get_ledger()
    text = user_input or ""

    m = _PIPELINE_ID_RE.search(text)
    if m:
        rec = ledger.get(m.group(1), user_id=user_id)
        return [rec] if rec else []

    cases = [c for c in _CASE_RE.findall(text) if "_" in c or "-" in c]
    if cases:
        found: list[PipelineRecord] = []
        for name in cases:
            found.extend(ledger.find_by_case(name, user_id=user_id))
        seen: set[str] = set()
        uniq: list[PipelineRecord] = []
        for r in found:
            if r.pipeline_id not in seen:
                seen.add(r.pipeline_id)
                uniq.append(r)
        return uniq

    # 「所有 / 全部」：直接最近若干条，交给查询聚合，不要再 interrupt
    if _ALL_RE.search(text):
        return ledger.list_recent(limit=10, user_id=user_id)

    if _LAST_RE.search(text):
        latest = ledger.latest(limit=1, user_id=user_id)
        if not latest:
            return []
        siblings = ledger.find_by_task(latest[0].task_id, user_id=user_id)
        return siblings or latest

    # 「第一条流水线」：按最近列表取第 N 条（list_recent 已按时间倒序）
    idx = _parse_ordinal(text)
    if idx is not None:
        recent = ledger.list_recent(limit=max(idx, 8), user_id=user_id)
        if 1 <= idx <= len(recent):
            return [recent[idx - 1]]
        return []

    return ledger.list_recent(limit=5, user_id=user_id)


def _match_pick(
    reply: str,
    candidates: list[PipelineRecord],
    *,
    user_id: str | None = None,
) -> PipelineRecord | None:
    """interrupt 回复：完整 id / 前缀 / 序号 / 唯一环境 IP。"""
    text = (reply or "").strip()
    if not text:
        return None

    # 完整 id
    chosen = next((r for r in candidates if r.pipeline_id == text), None)
    if chosen:
        return chosen
    # uuid 前缀（表格截断时常只看得见前几段）
    if len(text) >= 8:
        prefix_hits = [
            r for r in candidates if r.pipeline_id.lower().startswith(text.lower())
        ]
        if len(prefix_hits) == 1:
            return prefix_hits[0]

    idx = _parse_ordinal(text)
    if idx is not None and 1 <= idx <= len(candidates):
        return candidates[idx - 1]

    # 唯一环境
    env_hits = [r for r in candidates if r.env == text]
    if len(env_hits) == 1:
        return env_hits[0]

    from_ledger = get_ledger().get(text, user_id=user_id)
    if from_ledger:
        return from_ledger
    return None


def pick_records_for_action(
    user_input: str,
    *,
    action: str = "查询",
    user_id: str | None = None,
) -> list[PipelineRecord]:
    """
    消解 + 必要时 interrupt 让用户选。

    「所有」不过滤；同 task 多环境一并返回；跨 task 才问。

    参数:
        user_input: 本轮用户输入。
        action: 提示文案中的动作名（查询/启动/诊断）。
        user_id: 不为 None 时只在该用户名下的记录里消解/挑选。

    返回:
        最终选定的 PipelineRecord 列表；用户未选中时可能为空。
    """
    candidates = resolve_pipeline_records(user_input, user_id=user_id)
    if not candidates:
        return []

    text = user_input or ""
    # 用户已明确要全部 / 已用序号点名 / 「上次那几条」→ 不再追问
    if (
        len(candidates) == 1
        or _ALL_RE.search(text)
        or _LAST_RE.search(text)
        or _parse_ordinal(text) is not None
    ):
        return candidates

    if len(candidates) > 1:
        task_ids = {r.task_id for r in candidates}
        if len(task_ids) == 1:
            return candidates

        options = [
            {
                "pipeline_id": r.pipeline_id,
                "task_id": r.task_id,
                "cases": r.case_names,
                "version": r.version,
                "env": r.env,
                "status": r.status,
                "created_at": r.created_at,
            }
            for r in candidates[:8]
        ]
        reply = interrupt(
            {
                "type": "pick_run",
                "message": (
                    f"找到多条流水线，请输入序号（如 1 / 第一条）"
                    f"或 pipeline_id / 环境 IP 来{action}；"
                    f"也可说「全部」一起{action}"
                ),
                "options": options,
            }
        )
        reply_text = str(reply).strip()
        if _ALL_RE.search(reply_text) or reply_text in {"全部", "所有", "all"}:
            return candidates

        chosen = _match_pick(reply_text, candidates, user_id=user_id)
        if chosen is None:
            return []
        # 选中一条时：默认只查这一条（用户已明确点名）
        return [chosen]

    return candidates


def records_to_pipeline_dicts(records: list[PipelineRecord]) -> list[dict]:
    """
    把台账记录转成图状态里的 pipelines dict 列表。

    参数:
        records: 台账 PipelineRecord。

    返回:
        含 pipeline_id / case_names / version / env / status 的 dict 列表。
    """
    return [
        {
            "pipeline_id": r.pipeline_id,
            "case_names": r.case_names,
            "version": r.version,
            "env": r.env,
            "status": r.status,
            "error": "",
        }
        for r in records
    ]
