from work_agent.core.skills import SkillLoader, SkillPack
from work_agent.tools.mock.knowledge import MockKnowledgeSearchTool


def test_search_hits_aw_band():
    tool = MockKnowledgeSearchTool(use_embeddings=False)
    out = tool.search("CELL_BAND unresolved placeholder", top_k=2)
    assert "no hits" not in out
    assert "### " in out


def test_search_empty_query():
    assert "empty query" in MockKnowledgeSearchTool(use_embeddings=False).search("")


def test_search_no_hits():
    out = MockKnowledgeSearchTool(use_embeddings=False).search("zzzznotexist999")
    assert "no hits" in out


def test_select_references_retrieves_not_full_dump():
    pack = SkillPack(
        name="t",
        skill_md="s",
        template_md="t",
        references={
            "a.md": "## Band\n\nunresolved placeholder CELL_BAND missing\n",
            "b.md": "## Other\n\ncompletely unrelated topic xyz\n",
        },
    )
    refs = SkillLoader().select_references(
        pack, "CELL_BAND unresolved", top_k=1, use_embeddings=False
    )
    assert refs
    blob = "\n".join(refs.values())
    assert "CELL_BAND" in blob or "placeholder" in blob
    # 不应把无关文档整份塞进来（检索命中应偏向 a）
    assert len(refs) <= 2
