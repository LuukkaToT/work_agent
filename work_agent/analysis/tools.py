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
    """把检索结果压缩为 ToolMessage 可稳定传输的 JSON 字符串。"""

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
    """检索通用基础测试点库。

    dimensions 可填写 normal、boundary、abnormal、reconfiguration、
    recovery、performance。top_k 会被全局配置再次限流。
    """

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
    """检索指定信道的专属资料。

    allowed_channels 由 LangGraph 通过 InjectedState 注入，模型看不到也不能
    修改这条权限边界；越权请求返回结构化错误而不是读取任意目录。
    """

    state = state or {}
    canonical = normalize_channel(channel)
    allowed = {
        item
        for raw in (state.get("allowed_channels") or [])
        if (item := normalize_channel(str(raw)))
    }
    # 先标准化再检查白名单，防止别名和目录名称绕过当前任务的信道范围。
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
    # 只向研究子图暴露只读检索；文件写入和覆盖检查保持确定性代码调用。
    search_basic_test_points,
    search_channel_knowledge,
]
