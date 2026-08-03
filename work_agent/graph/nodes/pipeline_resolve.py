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


def resolve_pipeline_records(user_input: str) -> list[PipelineRecord]:
    ledger = get_ledger()
    text = user_input or ""

    m = _PIPELINE_ID_RE.search(text)
    if m:
        rec = ledger.get(m.group(1))
        return [rec] if rec else []

    cases = [c for c in _CASE_RE.findall(text) if "_" in c or "-" in c]
    if cases:
        found: list[PipelineRecord] = []
        for name in cases:
            found.extend(ledger.find_by_case(name))
        seen: set[str] = set()
        uniq: list[PipelineRecord] = []
        for r in found:
            if r.pipeline_id not in seen:
                seen.add(r.pipeline_id)
                uniq.append(r)
        return uniq

    if _LAST_RE.search(text):
        latest = ledger.latest(limit=1)
        if not latest:
            return []
        siblings = ledger.find_by_task(latest[0].task_id)
        return siblings or latest

    return ledger.list_recent(limit=5)


def pick_records_for_action(
    user_input: str,
    *,
    action: str = "查询",
) -> list[PipelineRecord]:
    """
    消解 + 必要时 interrupt 让用户选 pipeline_id。
    同 task 多环境记录一并返回。
    """
    candidates = resolve_pipeline_records(user_input)
    if not candidates:
        return []

    if len(candidates) > 1 and not _LAST_RE.search(user_input or ""):
        task_ids = {r.task_id for r in candidates}
        if len(task_ids) != 1:
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
                    "message": f"找到多条流水线，请输入要{action}的 pipeline_id",
                    "options": options,
                }
            )
            pid = str(reply).strip()
            chosen = next((r for r in candidates if r.pipeline_id == pid), None)
            if chosen is None:
                chosen = get_ledger().get(pid)
            if chosen is None:
                return []
            return get_ledger().find_by_task(chosen.task_id) or [chosen]
        return candidates

    return candidates


def records_to_pipeline_dicts(records: list[PipelineRecord]) -> list[dict]:
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
