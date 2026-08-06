"""仅供 DomainResearch 子图使用的受限只读检索 Tool。"""

from __future__ import annotations

from typing import Annotated, Any
import json

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from work_agent.analysis.corpus import (
    get_knowledge_retriever,
    normalize_channel,
)
from work_agent.analysis.schemas import EvidenceHit
from work_agent.core.config import get_settings


def _serialize_hits(hits: list[EvidenceHit]) -> str:
    # Tool 消息进入模型上下文，限制正文长度，完整 chunk 仍可由 chunk_id 追踪。
    payload = {
        "hit_count": len(hits),
        "hits": [
            {
                **hit.model_dump(mode="json", exclude={"content"}),
                "content": hit.content[:1000],
            }
            for hit in hits
        ],
    }
    return json.dumps(payload, ensure_ascii=False)


@tool
def search_basic_test_points(
    query: str,
    dimensions: list[str],
    top_k: int = 5,
    state: Annotated[dict[str, Any], InjectedState] = None,  # type: ignore[assignment]
) -> str:
    """检索通用基础测试点库。dimensions 可填写 normal、boundary、abnormal、reconfiguration、recovery、performance。"""
    _ = state
    limit = min(max(1, top_k), get_settings().test_analysis_top_k)
    hits = get_knowledge_retriever().search_basic(
        queries=[query],
        dimensions=dimensions,
        top_k=limit,
    )
    return _serialize_hits(hits)


@tool
def search_channel_knowledge(
    channel: str,
    query: str,
    top_k: int = 5,
    state: Annotated[dict[str, Any], InjectedState] = None,  # type: ignore[assignment]
) -> str:
    """检索指定信道的专属资料。channel 必须属于当前 DomainTask 的允许信道范围。"""
    state = state or {}
    canonical = normalize_channel(channel)
    allowed = {
        item
        for raw in (state.get("allowed_channels") or [])
        if (item := normalize_channel(str(raw)))
    }
    if not canonical or canonical not in allowed:
        return json.dumps(
            {
                "error": "channel_not_allowed",
                "requested": channel,
                "allowed_channels": sorted(allowed),
                "hits": [],
            },
            ensure_ascii=False,
        )

    limit = min(max(1, top_k), get_settings().test_analysis_top_k)
    hits = get_knowledge_retriever().search_channel(
        channel=canonical,
        queries=[query],
        top_k=limit,
    )
    return _serialize_hits(hits)


RESEARCH_TOOLS = [
    search_basic_test_points,
    search_channel_knowledge,
]
