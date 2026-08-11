"""Mock 知识检索：本地 kb + 混合 RAG（BM25∥Embedding→RRF）。"""

from __future__ import annotations

from pathlib import Path

from work_agent.core.retrieval import (
    HybridRetriever,
    format_hits,
    load_markdown_dir,
)


def _default_kb_dir() -> Path:
    """默认本地故障知识库目录。"""
    return Path(__file__).resolve().parent / "mock_5g_fault_kb" / "kb"


class MockKnowledgeSearchTool:
    """KnowledgeSearchTool：本地 markdown 混合检索。"""

    def __init__(
        self,
        kb_dir: Path | None = None,
        *,
        use_embeddings: bool = False,
        retriever: HybridRetriever | None = None,
    ) -> None:
        """
        构造本地混合检索工具。

        参数:
            kb_dir: kb 目录；None 用默认 mock_5g_fault_kb/kb。
            use_embeddings: 默认 False（单测/离线稳）；联调可 True。
            retriever: 可注入已建好的 HybridRetriever（单测用）。
        """
        self.kb_dir = kb_dir or _default_kb_dir()
        if retriever is not None:
            self._retriever = retriever
        else:
            chunks = load_markdown_dir(self.kb_dir)
            self._retriever = HybridRetriever(
                chunks,
                use_embeddings=use_embeddings,
            )

    def search(self, query: str, *, top_k: int = 3) -> str:
        """
        检索本地故障 kb。

        参数:
            query: 检索语句。
            top_k: 返回条数上限。

        返回:
            可读检索摘要；空查询或目录缺失时返回提示字符串。
        """
        q = (query or "").strip()
        if not q:
            return "[knowledge] empty query"
        if not self.kb_dir.is_dir() and not self._retriever.chunks:
            return f"[knowledge] kb dir missing: {self.kb_dir}"

        hits = self._retriever.search(
            q,
            top_k=top_k,
            pool_n=20,
            min_rrf_score=0.01,
        )
        return format_hits(hits, query=q)
