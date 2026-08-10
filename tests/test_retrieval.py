"""混合检索与 RRF。"""

from work_agent.core.retrieval import (
    BM25Index,
    Chunk,
    HybridRetriever,
    chunk_markdown,
    rrf_fuse,
    tokenize,
)


def test_chunk_markdown_by_headings():
    md = "# Title\n\n## A\n\nfoo CELL_BAND\n\n## B\n\nbar\n"
    chunks = chunk_markdown(md, source="t.md")
    assert len(chunks) >= 2
    assert any("CELL_BAND" in c.text for c in chunks)


def test_bm25_ranks_keyword():
    chunks = [
        Chunk("1", "a.md", "A", "unresolved placeholder CELL_BAND"),
        Chunk("2", "b.md", "B", "unrelated heartbeat ok"),
    ]
    r = HybridRetriever(chunks, use_embeddings=False)
    hits = r.search("CELL_BAND placeholder", top_k=1, pool_n=5, min_rrf_score=0.001)
    assert hits
    assert hits[0].chunk.chunk_id == "1"


def test_rrf_fuse_prefers_overlap():
    fused = rrf_fuse(
        [
            ["a", "b", "c"],
            ["c", "a", "d"],
        ],
        k=60,
    )
    # a 和 c 都出现在两路，应排在仅出现一路的前面
    ids = [cid for cid, _ in fused]
    assert ids[0] in ("a", "c")


def test_embedding_failure_falls_back_bm25():
    chunks = [
        Chunk("1", "a.md", "A", "protocol version mismatch"),
        Chunk("2", "b.md", "B", "noise"),
    ]

    def boom(_texts: list[str]) -> list[list[float]]:
        raise RuntimeError("no emb")

    r = HybridRetriever(chunks, embed_fn=boom, use_embeddings=True)
    assert r.embeddings_enabled is False
    hits = r.search("version mismatch", top_k=1, min_rrf_score=0.001)
    assert hits
    assert "mismatch" in hits[0].chunk.text


def test_tokenize_mixed():
    toks = tokenize("CELL_BAND 未解析 placeholder")
    assert "cell_band" in toks or "CELL_BAND".lower() in toks
    assert any("未解析" in t or t == "未解析" for t in toks) or "placeholder" in toks
