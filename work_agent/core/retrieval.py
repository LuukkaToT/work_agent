"""
本地混合检索：markdown 切块 + BM25 ∥ Embedding → RRF → top_k。

Embedding 不可用时自动降级为纯 BM25。不引入向量库。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


_TOKEN_RE = re.compile(r"[a-z0-9_]+|[\u4e00-\u9fff]+", re.IGNORECASE)
_HEADING_RE = re.compile(r"^(#{2,3})\s+(.+)$", re.MULTILINE)


@dataclass(frozen=True)
class Chunk:
    """检索单元。"""

    chunk_id: str
    source: str  # 文件名或逻辑名
    title_path: str
    text: str

    @property
    def indexed_text(self) -> str:
        """标题路径 + 正文，供 BM25 / embedding。"""
        if self.title_path:
            return f"{self.title_path}\n{self.text}"
        return self.text


@dataclass(frozen=True)
class RetrievalHit:
    """单条命中。"""

    chunk: Chunk
    score: float
    rank: int


def tokenize(text: str) -> list[str]:
    """
    简单中英混合分词（小写英数词 + 连续汉字）。

    参数:
        text: 原始文本。

    返回:
        token 列表。
    """
    return [m.group(0).lower() for m in _TOKEN_RE.finditer(text or "")]


def chunk_markdown(text: str, *, source: str = "") -> list[Chunk]:
    """
    按 ## / ### 切块；无标题则整篇一块。
    每块带 title_path（如 H2 > H3）。

    参数:
        text: markdown 正文。
        source: 来源名（通常为文件名），写入 chunk_id / source。

    返回:
        Chunk 列表（可含 preamble）。
    """
    text = (text or "").strip()
    if not text:
        return []

    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        return [
            Chunk(
                chunk_id=f"{source}::0",
                source=source,
                title_path="",
                text=text,
            )
        ]

    # 前言（第一个标题之前）
    chunks: list[Chunk] = []
    preamble = text[: matches[0].start()].strip()
    if preamble:
        chunks.append(
            Chunk(
                chunk_id=f"{source}::pre",
                source=source,
                title_path="(preamble)",
                text=preamble,
            )
        )

    stack: list[tuple[int, str]] = []  # (level, title)
    for i, m in enumerate(matches):
        level = len(m.group(1))
        title = m.group(2).strip()
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        title_path = " > ".join(t for _, t in stack)

        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if not body and not title:
            continue
        chunks.append(
            Chunk(
                chunk_id=f"{source}::{i}:{level}",
                source=source,
                title_path=title_path,
                text=body or title,
            )
        )
    return chunks


def load_markdown_dir(dir_path: Path) -> list[Chunk]:
    """
    加载目录下全部 .md 并切块。

    参数:
        dir_path: 含 markdown 的目录；不存在则返回空列表。

    返回:
        所有文件切出的 Chunk 列表。
    """
    if not dir_path.is_dir():
        return []
    out: list[Chunk] = []
    for path in sorted(dir_path.glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        out.extend(chunk_markdown(raw, source=path.name))
    return out


class BM25Index:
    """Okapi BM25（无第三方依赖）。"""

    def __init__(
        self, corpus: Sequence[list[str]], *, k1: float = 1.5, b: float = 0.75
    ) -> None:
        """
        参数:
            corpus: 已分词的文档列表。
            k1: TF 饱和参数。
            b: 文档长度归一化参数。
        """
        self.k1 = k1
        self.b = b
        self.corpus = [list(doc) for doc in corpus]
        self.n = len(self.corpus)
        self.doc_len = [len(doc) for doc in self.corpus]
        self.avgdl = (sum(self.doc_len) / self.n) if self.n else 0.0
        self.df: dict[str, int] = {}
        for doc in self.corpus:
            for t in set(doc):
                self.df[t] = self.df.get(t, 0) + 1

    def _idf(self, term: str) -> float:
        """计算 term 的平滑 IDF。"""
        df = self.df.get(term, 0)
        # 平滑 IDF
        return math.log(1 + (self.n - df + 0.5) / (df + 0.5))

    def scores(self, query_tokens: Sequence[str]) -> list[float]:
        """
        对语料每篇文档打 BM25 分。

        参数:
            query_tokens: 已分词的查询。

        返回:
            与语料等长的分数列表；空语料返回空列表。
        """
        if not self.n:
            return []
        scores = [0.0] * self.n
        q = [t for t in query_tokens if t]
        if not q:
            return scores
        for i, doc in enumerate(self.corpus):
            tf_map: dict[str, int] = {}
            for t in doc:
                tf_map[t] = tf_map.get(t, 0) + 1
            dl = self.doc_len[i] or 1
            s = 0.0
            for t in q:
                tf = tf_map.get(t, 0)
                if tf <= 0:
                    continue
                idf = self._idf(t)
                denom = tf + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1.0))
                s += idf * (tf * (self.k1 + 1)) / denom
            scores[i] = s
        return scores


def rrf_fuse(
    ranked_lists: Sequence[Sequence[str]],
    *,
    k: int = 60,
) -> list[tuple[str, float]]:
    """
    Reciprocal Rank Fusion，合并多路排序。

    参数:
        ranked_lists: 每路按名次排列的 chunk_id 列表（已是 topN）。
        k: RRF 常数（越大单路名次影响越平缓）。

    返回:
        ``(chunk_id, rrf_score)`` 降序列表。
    """
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, cid in enumerate(ranked, start=1):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda x: (-x[1], x[0]))


def _cosine(a: list[float], b: list[float]) -> float:
    """两向量余弦相似度；维数不合或零向量返回 0。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / (na * nb)


EmbedFn = Callable[[list[str]], list[list[float]]]


def default_embed_fn(texts: list[str]) -> list[list[float]]:
    """
    尝试走 OpenAI 兼容 embeddings；失败则抛异常由调用方降级。

    参数:
        texts: 待向量化文本列表。

    返回:
        与 texts 等长的向量列表。
    """
    from work_agent.core.config import get_settings

    s = get_settings()
    if not s.llm_api_key:
        raise RuntimeError("no api key for embeddings")

    # 延迟导入，单测可绕过
    from langchain_openai import OpenAIEmbeddings

    emb = OpenAIEmbeddings(
        model=getattr(s, "embedding_model", None)
        or "text-embedding-004",
        api_key=s.llm_api_key,
        base_url=s.llm_base_url,
    )
    # embed_documents 同步
    return emb.embed_documents(texts)


class HybridRetriever:
    """BM25 ∥ Embedding → RRF。"""

    def __init__(
        self,
        chunks: Sequence[Chunk],
        *,
        embed_fn: EmbedFn | None = None,
        use_embeddings: bool = True,
        rrf_k: int = 60,
    ) -> None:
        """
        参数:
            chunks: 已切好的检索单元。
            embed_fn: 自定义向量化；None 用 default_embed_fn。
            use_embeddings: False 时只用 BM25；True 但失败则自动降级。
            rrf_k: 传给 rrf_fuse 的常数。
        """
        self.chunks = list(chunks)
        self.by_id = {c.chunk_id: c for c in self.chunks}
        self.rrf_k = rrf_k
        self._embed_fn = embed_fn or default_embed_fn
        self._tokenized = [tokenize(c.indexed_text) for c in self.chunks]
        self._bm25 = BM25Index(self._tokenized)
        self._vectors: list[list[float]] | None = None
        self._embeddings_ok = False
        if use_embeddings and self.chunks:
            try:
                self._vectors = self._embed_fn([c.indexed_text for c in self.chunks])
                if len(self._vectors) == len(self.chunks):
                    self._embeddings_ok = True
                else:
                    self._vectors = None
            except Exception:  # noqa: BLE001
                self._vectors = None
                self._embeddings_ok = False

    @property
    def embeddings_enabled(self) -> bool:
        """本实例是否成功启用了向量检索。"""
        return self._embeddings_ok

    def _bm25_top(self, query: str, n: int) -> list[str]:
        """取 BM25 分数最高的前 n 个 chunk_id。"""
        toks = tokenize(query)
        scores = self._bm25.scores(toks)
        order = sorted(range(len(scores)), key=lambda i: (-scores[i], i))
        out: list[str] = []
        for i in order:
            if scores[i] <= 0:
                break
            out.append(self.chunks[i].chunk_id)
            if len(out) >= n:
                break
        return out

    def _emb_top(self, query: str, n: int) -> list[str]:
        """取与查询向量余弦相似度最高的前 n 个 chunk_id。"""
        if not self._embeddings_ok or not self._vectors:
            return []
        try:
            qv = self._embed_fn([query])[0]
        except Exception:  # noqa: BLE001
            return []
        scored = [
            (i, _cosine(qv, self._vectors[i]))
            for i in range(len(self.chunks))
        ]
        scored.sort(key=lambda x: (-x[1], x[0]))
        out: list[str] = []
        for i, sc in scored:
            if sc <= 0:
                break
            out.append(self.chunks[i].chunk_id)
            if len(out) >= n:
                break
        return out

    def search(
        self,
        query: str,
        *,
        top_k: int = 3,
        pool_n: int = 20,
        min_rrf_score: float = 0.01,
    ) -> list[RetrievalHit]:
        """
        混合检索：BM25（及可选 Embedding）经 RRF 融合后取 top_k。

        参数:
            query: 检索语句。
            top_k: 最终返回条数。
            pool_n: 每路召回池大小。
            min_rrf_score: 低于此 RRF 分的命中丢弃。

        返回:
            RetrievalHit 列表（按融合分降序）；空查询或空语料返回 []。
        """
        q = (query or "").strip()
        if not q or not self.chunks:
            return []

        bm25_ids = self._bm25_top(q, pool_n)
        lists: list[list[str]] = [bm25_ids]
        if self._embeddings_ok:
            emb_ids = self._emb_top(q, pool_n)
            if emb_ids:
                lists.append(emb_ids)

        fused = rrf_fuse(lists, k=self.rrf_k)
        hits: list[RetrievalHit] = []
        for rank, (cid, score) in enumerate(fused, start=1):
            if score < min_rrf_score:
                continue
            chunk = self.by_id.get(cid)
            if not chunk:
                continue
            hits.append(RetrievalHit(chunk=chunk, score=score, rank=rank))
            if len(hits) >= top_k:
                break
        return hits


def format_hits(hits: Sequence[RetrievalHit], *, query: str) -> str:
    """
    把命中格式化成给 LLM 读的文本。

    参数:
        hits: 检索命中。
        query: 原查询（写入头部说明）。

    返回:
        可读摘要；无命中时返回 no hits 提示。
    """
    if not hits:
        return f"[knowledge] no hits for query={query!r}"
    parts = [f"[knowledge] query={query!r} showing={len(hits)} mode=rrf"]
    for h in hits:
        c = h.chunk
        header = c.title_path or c.source
        parts.append(
            f"\n### {c.source} | {header} (rrf={h.score:.4f})\n{c.text[:800]}"
        )
    return "\n".join(parts)


def hits_to_reference_dict(hits: Sequence[RetrievalHit]) -> dict[str, str]:
    """
    供 SkillLoader.select_references：按 source 聚合块。

    参数:
        hits: 检索命中。

    返回:
        ``source::title_path`` → 正文；避免互相覆盖。
    """
    out: dict[str, str] = {}
    for h in hits:
        c = h.chunk
        key = f"{c.source}::{c.title_path or 'body'}"
        out[key] = c.text
    return out
