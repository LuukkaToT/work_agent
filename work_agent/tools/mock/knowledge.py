"""Mock 知识检索：扫 mock_5g_fault_kb/kb/*.md，简单关键词打分。"""

from __future__ import annotations

import re
from pathlib import Path


def _default_kb_dir() -> Path:
    """默认本地故障知识库目录。"""
    # 本文件在 work_agent/tools/mock/knowledge.py
    return Path(__file__).resolve().parent / "mock_5g_fault_kb" / "kb"


class MockKnowledgeSearchTool:
    """实现 KnowledgeSearchTool：本地 markdown 关键词检索。"""

    def __init__(self, kb_dir: Path | None = None) -> None:
        """
        参数:
            kb_dir: kb 目录；None 用默认 mock_5g_fault_kb/kb。
        """
        self.kb_dir = kb_dir or _default_kb_dir()

    def search(self, query: str, *, top_k: int = 3) -> str:
        """
        按关键词打分检索 kb 文档。

        参数:
            query: 检索语句。
            top_k: 返回条数上限。

        返回:
            可读检索摘要文本。
        """
        q = (query or "").strip()
        if not q:
            return "[knowledge] empty query"
        if not self.kb_dir.is_dir():
            return f"[knowledge] kb dir missing: {self.kb_dir}"

        tokens = [t for t in re.split(r"\s+|[,，;；]", q.lower()) if t]
        scored: list[tuple[int, str, str]] = []  # score, name, snippet

        for path in sorted(self.kb_dir.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            low = text.lower()
            score = 0
            for t in tokens:
                score += low.count(t)
            # 文件名也算分
            name_low = path.name.lower()
            for t in tokens:
                if t in name_low:
                    score += 3
            if score <= 0:
                continue
            snippet = self._snippet(text, tokens, max_chars=600)
            scored.append((score, path.name, snippet))

        if not scored:
            return f"[knowledge] no hits for query={q!r}"

        scored.sort(key=lambda x: (-x[0], x[1]))
        top = scored[: max(1, top_k)]
        parts = [f"[knowledge] query={q!r} hits={len(scored)} showing={len(top)}"]
        for score, name, snippet in top:
            parts.append(f"\n### {name} (score={score})\n{snippet}")
        return "\n".join(parts)

    @staticmethod
    def _snippet(text: str, tokens: list[str], *, max_chars: int) -> str:
        """在正文中截取靠近首个命中词的片段。"""
        if not tokens:
            return text[:max_chars]
        low = text.lower()
        pos = -1
        for t in tokens:
            i = low.find(t)
            if i >= 0 and (pos < 0 or i < pos):
                pos = i
        if pos < 0:
            return text[:max_chars]
        start = max(0, pos - 120)
        end = min(len(text), start + max_chars)
        chunk = text[start:end].strip()
        if start > 0:
            chunk = "..." + chunk
        if end < len(text):
            chunk = chunk + "..."
        return chunk
