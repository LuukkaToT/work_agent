from pathlib import Path

from work_agent.analysis.corpus import (
    ChannelKnowledgeRetriever,
    CorpusCatalog,
    normalize_channel,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_catalog_uses_folder_as_channel_metadata(tmp_path):
    _write(
        tmp_path / "channels" / "PUCCH" / "config.md",
        "# PUCCH 配置\n\n## 重配置\n\n检查资源切换和旧资源释放。",
    )
    _write(
        tmp_path / "basic_test_points" / "baseline.md",
        "# 基础测试点\n\n## 恢复\n\n检查异常后的业务恢复。",
    )

    catalog = CorpusCatalog(tmp_path)
    chunks = catalog.scan()

    assert catalog.available_channels() == ["PUCCH"]
    assert any(
        chunk.source_kind == "channel" and chunk.channel == "PUCCH"
        for chunk in chunks
    )
    assert any(chunk.source_kind == "basic" for chunk in chunks)


def test_retriever_hard_filters_channel_folder(tmp_path):
    _write(
        tmp_path / "PUCCH" / "a.md",
        "# PUCCH\n\n## 资源\n\nPUCCH resource reconfiguration。",
    )
    _write(
        tmp_path / "PUSCH" / "b.md",
        "# PUSCH\n\n## 资源\n\nPUSCH scheduling grant。",
    )
    retriever = ChannelKnowledgeRetriever(tmp_path)

    hits = retriever.search_channel(
        channel="PUCCH",
        queries=["resource reconfiguration"],
        top_k=5,
    )

    assert hits
    assert {hit.channel for hit in hits} == {"PUCCH"}
    assert all("PUCCH" in hit.relative_path for hit in hits)


def test_normalize_channel_accepts_common_aliases():
    assert normalize_channel("csi_rs") == "CSI-RS"
    assert normalize_channel("CSI-RS") == "CSI-RS"
    assert normalize_channel("pucch") == "PUCCH"
    assert normalize_channel("../../PUCCH") is None


def test_catalog_recognizes_channel_inside_prefixed_folder_name(tmp_path):
    _write(
        tmp_path / "01_PUCCH_上行控制信道" / "a.md",
        "# PUCCH\n\n## 配置\n\n测试资料。",
    )

    assert CorpusCatalog(tmp_path).available_channels() == ["PUCCH"]


def test_retrieval_reads_only_selected_scope(tmp_path, monkeypatch):
    _write(tmp_path / "PUCCH" / "a.md", "# PUCCH\n\n配置")
    _write(tmp_path / "PUSCH" / "b.md", "# PUSCH\n\n调度")
    retriever = ChannelKnowledgeRetriever(tmp_path)
    original = retriever.catalog._read_document
    opened = []

    def tracked(path):
        opened.append(path)
        return original(path)

    monkeypatch.setattr(retriever.catalog, "_read_document", tracked)

    retriever.search_channel(
        channel="PUCCH",
        queries=["配置"],
        top_k=3,
    )

    assert opened
    assert all("PUCCH" in str(path) for path in opened)
